"""Transport-neutral voice turn coordinator over the local WebSocket protocol."""
import asyncio
import base64
from contextlib import suppress
from time import perf_counter
from uuid import uuid4

from starlette.websockets import WebSocketDisconnect

from voice_agent.speech import DeepgramSpeech, SpeechError
from voice_agent.execution import ExecutionInterrupted


class VoiceSession:
    def __init__(self, ws, runtime, call_id, speech_factory=DeepgramSpeech):
        self.ws, self.runtime, self.call_id = ws, runtime, call_id
        self.speech = speech_factory(runtime.settings)
        self.queue = asyncio.Queue(maxsize=1)
        self.send_lock = asyncio.Lock()
        self.generation = 0
        self.closed = False
        self.heard_speech = False
        self.tts_task = None
        self.inflight = None
        self.seen_result_ids = []

    def log(self, kind, **detail):
        self.runtime.store.voice_event(self.call_id, {"type": kind, "provider": "deepgram", **detail})

    async def send(self, kind, **payload):
        if not self.closed:
            async with self.send_lock:
                await self.ws.send_json({"type": kind, **payload})

    async def interrupt(self):
        self.generation += 1
        self.runtime.interrupt(self.call_id)
        while not self.queue.empty():
            self.queue.get_nowait()
        if self.tts_task and not self.tts_task.done():
            self.tts_task.cancel()
        await self.send("interrupt", generation=self.generation)
        self.log("interrupted", generation=self.generation)

    async def receive_audio(self):
        total_bytes = 0
        start = perf_counter()
        while True:
            message = await self.ws.receive()
            if message["type"] == "websocket.disconnect":
                return
            pcm = message.get("bytes")
            if pcm is not None:
                if not pcm or len(pcm) > 32000 or len(pcm) % 2:
                    raise SpeechError("音频必须是 16kHz 单声道 PCM16，每帧最多 32000 字节。")
                total_bytes += len(pcm)
                if total_bytes > (perf_counter() - start + 5) * 64000:
                    raise SpeechError("音频发送速度超过实时处理限制。")
                self.speech.write(pcm)
            elif message.get("text"):
                import json
                try:
                    control = json.loads(message["text"])
                    if not isinstance(control, dict):
                        raise ValueError()
                except (ValueError, TypeError):
                    raise SpeechError("无效的语音控制消息。") from None
                if control.get("type") == "stop":
                    return
                if control.get("type") == "interrupt":
                    await self.interrupt()
                elif control.get("type") == "ping":
                    await self.send("pong")
                else:
                    raise SpeechError("不支持的语音控制消息。")

    async def recognition_events(self):
        while True:
            event = await self.speech.events.get()
            kind = event["type"]
            if kind == "error":
                raise SpeechError(event["message"])
            if kind in {"speech_started", "partial"}:
                if not self.heard_speech:
                    self.heard_speech = True
                    await self.interrupt()
                if kind == "partial":
                    await self.send("transcript", text=event.get("text", ""), final=False)
            elif kind == "final":
                text = event["text"].strip()
                if not text:
                    continue
                result_id = event.get("provider_result_id")
                if result_id and result_id in self.seen_result_ids:
                    continue
                if result_id:
                    self.seen_result_ids = [*self.seen_result_ids[-99:], result_id]
                if len(text) > 2000:
                    raise SpeechError("本轮语音过长，请分段表达。")
                if not self.heard_speech:
                    await self.interrupt()
                self.heard_speech = False
                request_id = "voice-" + str(uuid4())
                row = self.runtime.submit(self.call_id, text, request_id)
                while not self.queue.empty():
                    self.queue.get_nowait()
                self.queue.put_nowait((row, self.generation, False, perf_counter()))
                await self.send("transcript", text=text, final=True)
                self.log("stt_final", chars=len(text), request_id=request_id, provider_result_id=result_id)

    async def speak(self, texts, generation, trace_id, turn_started):
        start = perf_counter()
        count = 0
        started = False
        async def audio():
            if hasattr(self.speech, 'synthesize_stream'):
                async for chunk in self.speech.synthesize_stream(texts):
                    yield chunk
            else:
                async for text in texts:
                    async for chunk in self.speech.synthesize(text):
                        yield chunk
        try:
            async with asyncio.timeout(45):
                async for chunk in audio():
                    if generation != self.generation or self.closed:
                        return
                    if not started:
                        started = True
                        await self.send('audio_start', generation=generation,sample_rate=16000,encoding='pcm_s16le')
                        self.log('first_audio',trace_id=trace_id,stt_final_to_first_audio_ms=round((perf_counter()-turn_started)*1000,2))
                    count += len(chunk)
                    await self.send("audio", generation=generation, data=base64.b64encode(chunk).decode())
            self.log("tts_complete", trace_id=trace_id, audio_bytes=count, duration_ms=round((perf_counter()-start)*1000, 2))
            await self.send("audio_end", generation=generation)
        except asyncio.CancelledError:
            self.log("tts_interrupted", trace_id=trace_id, audio_bytes=count)
            raise
        except Exception:
            self.log("tts_error", trace_id=trace_id)
            await self.send("error", code="tts_error", message="语音合成失败，业务结果已保留。请检查 Deepgram 配置和网络。")

    async def process_turns(self):
        while True:
            row, generation, resume, turn_started = await self.queue.get()
            request_id = row["request_id"]
            if generation != self.generation:
                continue
            await self.send("status", status="thinking")
            chunks = asyncio.Queue(maxsize=32)
            published = []
            loop = asyncio.get_running_loop()
            def accept(text, expected=generation, queue=chunks, seen=published):
                if expected == self.generation and not self.closed and text not in seen:
                    seen.append(text)
                    queue.put_nowait(text)
            def on_reply(text, receiver=accept):
                loop.call_soon_threadsafe(receiver,text)
            async def texts(queue=chunks, expected=generation, trace_id=row['trace_id']):
                while True:
                    text = await queue.get()
                    if text is None:
                        return
                    await self.send('reply_chunk',text=text,generation=expected,trace_id=trace_id)
                    self.log('reply_chunk',trace_id=trace_id,chars=len(text))
                    yield text
            self.tts_task = asyncio.create_task(self.speak(texts(),generation,row['trace_id'],turn_started))
            self.inflight = asyncio.create_task(asyncio.to_thread(self.runtime.execute,row,resume=resume,on_reply=on_reply))
            try:
                response = await asyncio.shield(self.inflight)
            except asyncio.CancelledError:
                with suppress(Exception):
                    await self.inflight
                raise
            except ExecutionInterrupted:
                self.tts_task.cancel()
                await asyncio.gather(self.tts_task,return_exceptions=True)
                self.log("task_interrupted", request_id=request_id)
                continue
            except Exception:
                self.tts_task.cancel()
                await asyncio.gather(self.tts_task,return_exceptions=True)
                self.log("turn_error", request_id=request_id)
                await self.send("error", code="turn_error", message="业务处理发生异常，请核验状态；不要假定操作已成功。")
                continue
            self.log("turn_complete", request_id=request_id, trace_id=response["trace_id"])
            if generation != self.generation or self.closed:
                self.tts_task.cancel()
                continue
            await self.send("turn_result", response=response)
            if not self.closed and generation == self.generation:
                # on_reply callbacks are queued before the worker future completes.
                remaining = response['state']['reply']
                for text in published:
                    remaining = remaining.replace(text,'',1)
                if remaining.strip():
                    await chunks.put(remaining.strip())
                await chunks.put(None)
                try:
                    await self.tts_task
                except asyncio.CancelledError:
                    if asyncio.current_task().cancelling():
                        raise
            if self.queue.empty():
                await self.send("status", status="listening")

    async def run(self):
        tasks = []
        try:
            await self.speech.start()
            self.log("voice_connected", **self.runtime.settings.speech_status())
            await self.send("ready", **self.runtime.settings.speech_status())
            pending = self.runtime.journal.pending(self.call_id)
            if pending:
                self.queue.put_nowait((pending, self.generation, True, perf_counter()))
                await self.send("status", status="resuming")
            tasks = [asyncio.create_task(self.receive_audio()), asyncio.create_task(self.recognition_events()), asyncio.create_task(self.process_turns())]
            async with asyncio.timeout(1200):
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    task.result()
        except WebSocketDisconnect:
            pass
        except SpeechError as exc:
            self.log("voice_error", error_type=type(exc).__name__)
            with suppress(Exception):
                await self.send("error", code="voice_error", message=str(exc))
        except Exception as exc:
            self.log("voice_error", error_type=type(exc).__name__)
            with suppress(Exception):
                await self.send("error", code="voice_error", message="语音连接异常或达到 20 分钟限制，请重新连接并检查 Deepgram 配置。")
        finally:
            self.runtime.interrupt(self.call_id)
            self.closed = True
            if self.tts_task:
                self.tts_task.cancel()
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            await self.speech.close()
            self.log("voice_disconnected")
            with suppress(Exception):
                await self.ws.close()
