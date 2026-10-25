import asyncio

from fastapi.testclient import TestClient
import pytest
from starlette.websockets import WebSocketDisconnect

from voice_agent.api import create_app
from voice_agent.config import Settings


class FakeSpeech:
    """Simulated cloud boundary: real coordinator, runtime, business DB and WS."""
    def __init__(self, settings):
        self.events = asyncio.Queue()
        self.writes = 0

    async def start(self):
        pass

    def write(self, audio):
        self.writes += 1
        if self.writes == 1:
            self.events.put_nowait({"type": "speech_started"})
            self.events.put_nowait({"type": "partial", "text": "I want a human"})
            self.events.put_nowait({"type": "final", "text": "I want a human", "provider_result_id": "synthetic-1"})
            self.events.put_nowait({"type": "final", "text": "I want a human", "provider_result_id": "synthetic-1"})

    async def synthesize(self, text):
        assert "human escalation" in text
        yield b"\x01\x00" * 320
        yield b"\x02\x00" * 320

    async def close(self):
        pass


@pytest.fixture
def configured(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPGRAM_API_KEY", "synthetic-speech-key")
    return Settings(data_dir=tmp_path)


def make_call(client):
    return client.post("/api/calls").json()["state"]["call_id"]


def receive_until(ws, kind):
    received = []
    for _ in range(30):
        item = ws.receive_json()
        received.append(item)
        if item["type"] == kind:
            return received
    raise AssertionError(f"Did not receive {kind}: {received}")


def test_audio_transcription_workflow_and_tts_share_real_trace(configured):
    with TestClient(create_app(configured, speech_factory=FakeSpeech)) as client:
        call_id = make_call(client)
        with client.websocket_connect(f"/api/calls/{call_id}/voice") as ws:
            ready = ws.receive_json()
            assert ready["type"] == "ready"
            assert ready["sample_rate"] == 16000
            ws.send_bytes(b"\x00\x00" * 320)
            events = receive_until(ws, "audio_end")
            result = next(e["response"] for e in events if e["type"] == "turn_result")
            assert result["state"]["status"] == "human_escalation"
            assert len([e for e in events if e["type"] == "audio"]) == 2
            assert len([e for e in events if e["type"] == "transcript" and e["final"]]) == 1
            assert client.post(f"/api/calls/{call_id}/turns", json={"message": "hi"}).status_code == 409
            ws.send_json({"type": "stop"})
        snapshot = client.get(f"/api/calls/{call_id}").json()
        assert len(snapshot["outcomes"]["transfers"]) == 1
        logs = snapshot["voice_events"]
        assert any(e["type"] == "tts_complete" and e["trace_id"] == result["trace_id"] for e in logs)
        assert "synthetic-speech-key" not in str(snapshot)


def test_missing_credentials_fail_explicitly_without_cloud_access(tmp_path, monkeypatch):
    monkeypatch.delenv("DEEPGRAM_API_KEY", raising=False)
    with TestClient(create_app(Settings(data_dir=tmp_path))) as client:
        call_id = make_call(client)
        assert client.get("/health").json()["speech"]["configured"] is False
        with client.websocket_connect(f"/api/calls/{call_id}/voice") as ws:
            assert ws.receive_json()["code"] == "speech_not_configured"


def test_invalid_audio_rejected_before_business_execution(configured):
    with TestClient(create_app(configured, speech_factory=FakeSpeech)) as client:
        call_id = make_call(client)
        with client.websocket_connect(f"/api/calls/{call_id}/voice") as ws:
            ws.receive_json()
            ws.send_bytes(b"\x00")
            assert ws.receive_json()["code"] == "voice_error"
        assert client.get(f"/api/calls/{call_id}").json()["traces"] == []


def test_provider_error_is_reported_instead_of_text_fallback(configured):
    class BrokenSpeech(FakeSpeech):
        async def start(self):
            self.events.put_nowait({"type": "error", "message": "合成测试：服务不可用"})
    with TestClient(create_app(configured, speech_factory=BrokenSpeech)) as client:
        call_id = make_call(client)
        with client.websocket_connect(f"/api/calls/{call_id}/voice") as ws:
            ws.receive_json()
            assert ws.receive_json()["type"] == "error"


def test_interrupt_stops_synthesis_and_disconnect_finishes_cleanly(configured):
    class SlowSpeech(FakeSpeech):
        async def synthesize(self, text):
            yield b"\x00\x00" * 320
            await asyncio.sleep(30)
            yield b"\x01\x00" * 320
    with TestClient(create_app(configured, speech_factory=SlowSpeech)) as client:
        call_id = make_call(client)
        with client.websocket_connect(f"/api/calls/{call_id}/voice") as ws:
            ws.receive_json()
            ws.send_bytes(b"\x00\x00" * 320)
            receive_until(ws, "audio")
            ws.send_json({"type": "interrupt"})
            receive_until(ws, "interrupt")
            ws.send_json({"type": "stop"})
        events = client.get(f"/api/calls/{call_id}").json()["voice_events"]
        assert any(e["type"] == "tts_interrupted" for e in events)
        assert any(e["type"] == "voice_disconnected" for e in events)


def test_cross_origin_socket_is_rejected(configured):
    with TestClient(create_app(configured, speech_factory=FakeSpeech)) as client:
        call_id = make_call(client)
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(f"/api/calls/{call_id}/voice", headers={"origin": "https://other.example"}):
                pass


def test_only_one_audio_connection_per_call(configured):
    with TestClient(create_app(configured, speech_factory=FakeSpeech)) as client:
        call_id = make_call(client)
        with client.websocket_connect(f"/api/calls/{call_id}/voice") as first:
            first.receive_json()
            with client.websocket_connect(f"/api/calls/{call_id}/voice") as second:
                assert second.receive_json()["code"] == "already_connected"
            first.send_json({"type": "stop"})


def test_initialization_error_is_explicit_and_does_not_leak_secrets(configured):
    def broken_factory(settings):
        raise RuntimeError("synthetic-speech-key")
    with TestClient(create_app(configured, speech_factory=broken_factory)) as client:
        call_id = make_call(client)
        with client.websocket_connect(f"/api/calls/{call_id}/voice") as ws:
            result = ws.receive_json()
            assert result["code"] == "speech_init_failed"
            assert "synthetic-speech-key" not in str(result)


def test_reconnect_same_call_automatically_resumes_pending_task(configured):
    with TestClient(create_app(configured, speech_factory=FakeSpeech)) as client:
        call_id = make_call(client)
        runtime = client.app.state.runtime
        runtime.submit(call_id, 'I want a human', 'pending-before-disconnect')
        assert client.post(f'/api/calls/{call_id}/suspend').status_code == 200
        assert client.get(f'/api/calls/{call_id}').json()['execution']['status'] == 'suspended'
        with client.websocket_connect(f'/api/calls/{call_id}/voice') as ws:
            assert ws.receive_json()['type'] == 'ready'
            events = receive_until(ws, 'audio_end')
            assert any(e.get('status') == 'resuming' for e in events)
            result = next(e['response'] for e in events if e['type'] == 'turn_result')
            assert result['state']['status'] == 'human_escalation'
            ws.send_json({'type': 'stop'})
        assert client.get(f'/api/calls/{call_id}').json()['execution'] is None
        assert runtime.store.traces(call_id)[0]['resumed'] is True
        assert len(runtime.store.outcomes(call_id)['transfers']) == 1


def test_latest_queued_utterance_replaces_previous_one(configured):
    class BurstSpeech(FakeSpeech):
        def write(self, audio):
            self.writes += 1
            if self.writes == 1:
                self.events.put_nowait({'type': 'final', 'text': 'Book a cleaning', 'provider_result_id': 'old'})
                self.events.put_nowait({'type': 'final', 'text': 'I want a human', 'provider_result_id': 'new'})
    with TestClient(create_app(configured, speech_factory=BurstSpeech)) as client:
        call_id = make_call(client)
        with client.websocket_connect(f'/api/calls/{call_id}/voice') as ws:
            ws.receive_json()
            ws.send_bytes(b'\x00\x00' * 320)
            events = receive_until(ws, 'audio_end')
            results = [e['response'] for e in events if e['type'] == 'turn_result']
            assert len(results) == 1
            assert results[0]['state']['status'] == 'human_escalation'
            assert results[0]['state']['message'] == 'I want a human'
            ws.send_json({'type': 'stop'})
        assert len(client.app.state.runtime.store.traces(call_id)) == 1
