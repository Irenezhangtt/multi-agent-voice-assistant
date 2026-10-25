"""Deepgram STT/TTS via outbound WebSockets; no Voice Agent API."""
import asyncio
from contextlib import suppress
import json
import os
from typing import AsyncIterator
from urllib.parse import urlencode

from websockets.asyncio.client import connect


class SpeechError(RuntimeError):
    pass


class DeepgramSpeech:
    def __init__(self, settings):
        self.settings = settings
        self.key = os.getenv("DEEPGRAM_API_KEY", "").strip()
        if not self.key:
            raise SpeechError("请在本机 .env 配置 DEEPGRAM_API_KEY，然后重启服务。")
        self.events = asyncio.Queue(maxsize=128)
        self.audio = asyncio.Queue(maxsize=100)
        self.socket = None
        self.tasks = []
        self.closed = False
        self.fragments = []
        self.seen_segments = []
        self.turn_number = 0
        self.request_id = "deepgram"

    def emit(self, event):
        if self.closed:
            return
        if self.events.full():
            while not self.events.empty():
                self.events.get_nowait()
            self.events.put_nowait({"type": "error", "message": "语音事件处理过慢，请重新连接。"})
        else:
            self.events.put_nowait(event)

    def finish_utterance(self):
        text = " ".join(self.fragments).strip()
        self.fragments.clear()
        if text:
            self.turn_number += 1
            self.emit({"type": "final", "text": text, "provider_result_id": f"{self.request_id}:{self.turn_number}"})

    def handle_stt_message(self, message):
        kind = message.get("type")
        if kind == "Metadata":
            self.request_id = message.get("request_id", self.request_id)
        elif kind == "SpeechStarted":
            self.emit({"type": "speech_started"})
        elif kind == "UtteranceEnd":
            self.finish_utterance()
        elif kind == "Error":
            raise SpeechError("Deepgram STT 返回错误，请检查模型、语言、额度和配置。")
        elif kind == "Results":
            alternatives = message.get("channel", {}).get("alternatives", [])
            text = alternatives[0].get("transcript", "").strip() if alternatives else ""
            self.request_id = message.get("metadata", {}).get("request_id", self.request_id)
            if message.get("is_final"):
                # is_final commits a segment; speech_final ends a whole turn.
                if text:
                    identity = (message.get("start"), message.get("duration"), text)
                    if identity not in self.seen_segments:
                        self.seen_segments = [*self.seen_segments[-511:], identity]
                        self.fragments.append(text)
                if message.get("speech_final"):
                    self.finish_utterance()
                elif text:
                    self.emit({"type": "partial", "text": " ".join(self.fragments)})
            elif text:
                self.emit({"type": "partial", "text": " ".join([*self.fragments, text])})

    async def start(self):
        params = {
            "model": self.settings.stt_model, "language": self.settings.speech_language,
            "encoding": "linear16", "sample_rate": 16000, "channels": 1,
            "interim_results": "true", "punctuate": "true", "smart_format": "true",
            "vad_events": "true", "endpointing": self.settings.speech_endpointing_ms,
            "utterance_end_ms": 1000,
        }
        try:
            self.socket = await connect(
                "wss://api.deepgram.com/v1/listen?" + urlencode(params),
                additional_headers={"Authorization": f"Token {self.key}"},
                open_timeout=12, close_timeout=2, max_size=2**20, max_queue=32,
            )
        except Exception:
            raise SpeechError("无法连接 Deepgram STT，请检查 Key、模型、语言、额度和网络。") from None
        self.tasks = [asyncio.create_task(self.receive()), asyncio.create_task(self.send_audio()), asyncio.create_task(self.keepalive())]

    def write(self, pcm: bytes):
        if self.closed or self.socket is None:
            raise SpeechError("语音识别连接未就绪。")
        try:
            self.audio.put_nowait(pcm)
        except asyncio.QueueFull:
            raise SpeechError("语音上传队列已满，请检查网络后重新连接。") from None

    async def receive(self):
        try:
            async for message in self.socket:
                if isinstance(message, str):
                    self.handle_stt_message(json.loads(message))
            if not self.closed:
                self.emit({"type": "error", "message": "Deepgram 识别连接已结束，请重新连接。"})
        except asyncio.CancelledError:
            raise
        except Exception:
            self.emit({"type": "error", "message": "Deepgram 识别连接异常，请检查配置和网络。"})

    async def send_audio(self):
        try:
            while True:
                await self.socket.send(await self.audio.get())
        except asyncio.CancelledError:
            raise
        except Exception:
            self.emit({"type": "error", "message": "Deepgram 音频上传失败，请重新连接。"})

    async def keepalive(self):
        try:
            while True:
                await asyncio.sleep(4)
                await self.socket.send(json.dumps({"type": "KeepAlive"}))
        except asyncio.CancelledError:
            raise
        except Exception:
            self.emit({"type": "error", "message": "Deepgram 连接保活失败，请重新连接。"})

    async def close(self):
        self.closed = True
        for task in self.tasks:
            task.cancel()
        if self.tasks:
            await asyncio.gather(*self.tasks, return_exceptions=True)
        if self.socket:
            with suppress(Exception):
                await self.socket.close()

    async def synthesize(self, text: str) -> AsyncIterator[bytes]:
        async def texts():
            if text.strip():
                yield text
        async for chunk in self.synthesize_stream(texts()):
            yield chunk

    async def synthesize_stream(self, texts: AsyncIterator[str]) -> AsyncIterator[bytes]:
        """Duplex TTS: receive audio while later verified text is still arriving.

        Flush the first sentence promptly, then flush the remaining response once
        at EOF. This avoids flushing every token/sentence and hitting Aura limits.
        """
        import re
        params = {"model": self.settings.speech_voice, "encoding": "linear16", "sample_rate": 16000}
        try:
            async with connect(
                "wss://api.deepgram.com/v1/speak?" + urlencode(params),
                additional_headers={"Authorization": f"Token {self.key}"},
                open_timeout=12, close_timeout=2, max_size=2**20, max_queue=32,
            ) as socket:
                events = asyncio.Queue(maxsize=32)
                requested, flushed, finished = 0, 0, False

                async def produce():
                    nonlocal requested
                    pending = False
                    try:
                        async for text in texts:
                            for sentence in re.split(r'(?<=[.!?])\s+', text.strip()):
                                if not sentence:
                                    continue
                                await socket.send(json.dumps({"type":"Speak", "text":sentence + (" " if sentence[-1] in '.!?' else '')}, ensure_ascii=False))
                                pending = True
                                if requested == 0:
                                    requested += 1
                                    await socket.send(json.dumps({"type":"Flush"}))
                                    pending = False
                        if pending:
                            requested += 1
                            await socket.send(json.dumps({"type":"Flush"}))
                        await events.put(('done', None))
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        await events.put(('error', None))

                async def receive():
                    try:
                        async for message in socket:
                            if isinstance(message, bytes):
                                await events.put(('audio',message))
                            else:
                                event = json.loads(message)
                                if event.get('type') == 'Flushed':
                                    await events.put(('flushed',None))
                                elif event.get('type') == 'Error':
                                    await events.put(('error',None))
                        await events.put(('closed',None))
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        await events.put(('error',None))

                tasks = [asyncio.create_task(produce()), asyncio.create_task(receive())]
                try:
                    async with asyncio.timeout(45):
                        while True:
                            kind, value = await events.get()
                            if kind == 'audio' and value:
                                yield value
                            elif kind == 'flushed':
                                flushed += 1
                            elif kind == 'done':
                                finished = True
                            elif kind in {'error','closed'}:
                                raise SpeechError('Deepgram TTS ended before completing the text stream')
                            if finished and flushed >= requested:
                                return
                except asyncio.CancelledError:
                    with suppress(Exception):
                        await asyncio.wait_for(socket.send(json.dumps({"type":"Clear"})),timeout=.5)
                    raise
                finally:
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(*tasks,return_exceptions=True)
        except asyncio.CancelledError:
            raise
        except Exception:
            raise SpeechError("Deepgram 语音合成失败，请检查 Key、声音模型、额度和网络。") from None
