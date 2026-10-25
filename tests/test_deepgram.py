"""Exercise the real Deepgram adapter against a fake WebSocket boundary."""
import asyncio
import json
from urllib.parse import parse_qs, urlparse

import pytest

from voice_agent.config import Settings
from voice_agent.speech import DeepgramSpeech, SpeechError


class Socket:
    def __init__(self, messages=()):
        self.messages = asyncio.Queue()
        for message in messages:
            self.messages.put_nowait(message)
        self.sent = []
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        message = await self.messages.get()
        if message is None:
            raise StopAsyncIteration
        return message

    async def send(self, message):
        self.sent.append(message)

    async def close(self):
        self.closed = True


class Connection:
    def __init__(self, socket):
        self.socket = socket

    def __await__(self):
        async def opened():
            return self.socket
        return opened().__await__()

    async def __aenter__(self):
        return self.socket

    async def __aexit__(self, *args):
        await self.socket.close()


@pytest.fixture
def speech(monkeypatch):
    monkeypatch.setenv("DEEPGRAM_API_KEY", "synthetic-key")
    return DeepgramSpeech(Settings())


def segment(text, start=0, *, final=True, end=False):
    return {"type": "Results", "start": start, "duration": 1,
            "is_final": final, "speech_final": end,
            "channel": {"alternatives": [{"transcript": text}]}}


def test_segments_are_aggregated_until_turn_boundary(speech):
    speech.handle_stt_message(segment("Book an appointment"))
    speech.handle_stt_message(segment("tomorrow", 1, final=False))
    speech.handle_stt_message(segment("tomorrow", 1, end=True))
    speech.handle_stt_message({"type": "UtteranceEnd"})
    events = []
    while not speech.events.empty():
        events.append(speech.events.get_nowait())
    finals = [e for e in events if e["type"] == "final"]
    assert [e["text"] for e in finals] == ["Book an appointment tomorrow"]
    assert len({e["provider_result_id"] for e in finals}) == 1


def test_duplicate_segment_can_still_end_turn(speech):
    speech.handle_stt_message(segment("Hello"))
    speech.handle_stt_message(segment("Hello", end=True))
    assert speech.events.get_nowait()["type"] == "partial"
    assert speech.events.get_nowait()["text"] == "Hello"
    assert speech.events.empty()


def test_stt_pcm_parameters_audio_forwarding_and_cleanup(speech, monkeypatch):
    socket, calls = Socket(), []
    def connect(url, **kwargs):
        calls.append((url, kwargs))
        return Connection(socket)
    monkeypatch.setattr("voice_agent.speech.connect", connect)

    async def run():
        await speech.start()
        speech.write(b"\x01\x00" * 320)
        await asyncio.sleep(.01)
        socket.messages.put_nowait(json.dumps(segment("Hello", end=True)))
        result = await asyncio.wait_for(speech.events.get(), 1)
        assert result["text"] == "Hello"
        await speech.close()
        assert all(task.done() for task in speech.tasks)
    asyncio.run(run())
    url, options = calls[0]
    assert urlparse(url).path == "/v1/listen"
    params = parse_qs(urlparse(url).query)
    assert params["encoding"] == ["linear16"]
    assert params["sample_rate"] == ["16000"]
    assert params["model"] == ["nova-3"]
    assert params["language"] == ["en"]
    assert options["additional_headers"] == {"Authorization": "Token synthetic-key"}
    assert "synthetic-key" not in url
    assert socket.sent == [b"\x01\x00" * 320]
    assert socket.closed


def test_tts_sends_speak_flush_and_streams_raw_pcm(speech, monkeypatch):
    pcm = b"\x01\x00" * 320
    socket = Socket([json.dumps({"type": "Metadata"}), pcm, json.dumps({"type": "Flushed"})])
    calls = []
    def connect(url, **kwargs):
        calls.append(url)
        return Connection(socket)
    monkeypatch.setattr("voice_agent.speech.connect", connect)
    async def run():
        assert [chunk async for chunk in speech.synthesize("Hello")] == [pcm]
    asyncio.run(run())
    assert [json.loads(m) for m in socket.sent] == [{"type": "Speak", "text": "Hello"}, {"type": "Flush"}]
    assert parse_qs(urlparse(calls[0]).query)["model"] == ["aura-2-thalia-en"]
    assert socket.closed


def test_tts_cancellation_clears_remote_audio(speech, monkeypatch):
    socket = Socket([b"\x00\x00"])
    monkeypatch.setattr("voice_agent.speech.connect", lambda *a, **k: Connection(socket))
    async def run():
        first = asyncio.Event()
        async def consume():
            async for chunk in speech.synthesize("Hello"):
                first.set()
        task = asyncio.create_task(consume())
        await asyncio.wait_for(first.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(run())
    assert json.loads(socket.sent[-1]) == {"type": "Clear"}
    assert socket.closed


def test_tts_premature_close_and_connection_errors_are_safe(speech, monkeypatch):
    socket = Socket([None])
    monkeypatch.setattr("voice_agent.speech.connect", lambda *a, **k: Connection(socket))
    async def run():
        with pytest.raises(SpeechError) as error:
            _ = [chunk async for chunk in speech.synthesize("Hello")]
        assert "synthetic-key" not in str(error.value)
        def broken(*args, **kwargs):
            raise RuntimeError("synthetic-key")
        monkeypatch.setattr("voice_agent.speech.connect", broken)
        with pytest.raises(SpeechError) as error:
            await speech.start()
        assert "synthetic-key" not in str(error.value)
    asyncio.run(run())


def test_duplex_tts_first_audio_arrives_before_text_producer_finishes(speech,monkeypatch):
    class LiveSocket(Socket):
        async def send(self,message):
            await super().send(message)
            if json.loads(message)['type']=='Flush':
                self.messages.put_nowait(b'\x01\x00')
                self.messages.put_nowait(json.dumps({'type':'Flushed'}))
    socket=LiveSocket()
    connections=[]
    def connect(*args,**kwargs):
        connections.append(1)
        return Connection(socket)
    monkeypatch.setattr('voice_agent.speech.connect',connect)
    async def run():
        first_audio=asyncio.Event()
        finished=False
        async def texts():
            nonlocal finished
            yield 'First verified sentence.'
            await asyncio.wait_for(first_audio.wait(),1)
            yield 'Second verified sentence.'
            finished=True
        chunks=[]
        async for chunk in speech.synthesize_stream(texts()):
            if not chunks:
                assert not finished
                first_audio.set()
            chunks.append(chunk)
        assert len(chunks)==2
    asyncio.run(run())
    assert len(connections)==1
    assert sum(json.loads(m)['type']=='Flush' for m in socket.sent)==2
