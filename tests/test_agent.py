from datetime import datetime
import json
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest

from voice_agent.api import create_app
from voice_agent.config import Settings
from voice_agent.runtime import AgentRuntime


NOW = datetime.fromisoformat("2026-10-23T09:00:00-07:00")
REQUEST = "预约洗牙，姓名：小林，2026-11-03 14:00，电话：2025550100"


@pytest.fixture
def runtime(tmp_path):
    instance = AgentRuntime(Settings(data_dir=tmp_path), clock=lambda: NOW)
    yield instance
    instance.close()


def test_duplicate_http_request_is_replayed_without_duplicate_write(runtime):
    call_id = runtime.create_call()["state"]["call_id"]
    runtime.turn(call_id, REQUEST)
    request_id = str(uuid4())
    first = runtime.turn(call_id, "确认预约", request_id)
    second = runtime.turn(call_id, "确认预约", request_id)
    assert first == second
    assert len(runtime.store.outcomes(call_id)["bookings"]) == 1
    assert len(runtime.store.traces(call_id)) == 2
    with pytest.raises(ValueError, match="request_id"):
        runtime.turn(call_id, "转人工", request_id)


def test_english_booking_insurance_and_handoff_replies(runtime):
    call_id = runtime.create_call()["state"]["call_id"]
    offered = runtime.turn(call_id, "Book a cleaning, name: Alex, 2026-11-03 14:00, phone: 2025550100")
    assert offered["state"]["status"] == "awaiting_confirmation"
    assert "Say confirm booking" in offered["state"]["reply"]
    booked = runtime.turn(call_id, "Confirm booking.")
    assert booked["state"]["booking"]["status"] == "confirmed"
    checked = runtime.turn(call_id, "Check insurance: Aetna, member ID: DEMO123, cleaning")
    assert checked["state"]["insurance"]["status"] == "active"
    assert "does not guarantee coverage" in checked["state"]["reply"]
    transferred = runtime.turn(call_id, "I want to speak to a human.")
    assert "does not connect a real phone call" in transferred["state"]["reply"]
    assert len(runtime.store.outcomes(call_id)["bookings"]) == 1


def test_call_survives_process_restart(tmp_path):
    settings = Settings(data_dir=tmp_path)
    first = AgentRuntime(settings, clock=lambda: NOW)
    call_id = first.create_call()["state"]["call_id"]
    first.turn(call_id, REQUEST)
    first.close()
    second = AgentRuntime(settings, clock=lambda: NOW)
    try:
        result = second.turn(call_id, "确认预约")
        assert result["state"]["booking"]["status"] == "confirmed"
        assert len(second.store.outcomes(call_id)["bookings"]) == 1
    finally:
        second.close()


def test_two_callers_cannot_book_same_slot(runtime):
    first = runtime.create_call()["state"]["call_id"]
    second = runtime.create_call()["state"]["call_id"]
    runtime.turn(first, REQUEST)
    runtime.turn(second, REQUEST.replace("小林", "小王").replace("2025550100", "2025550101"))
    runtime.turn(first, "确认预约")
    result = runtime.turn(second, "确认预约")
    assert result["state"]["status"] == "needs_information"
    assert runtime.store.outcomes(second)["bookings"] == []


def test_call_versions_are_checked_before_continuation(tmp_path):
    first = AgentRuntime(Settings(data_dir=tmp_path), clock=lambda: NOW)
    call_id = first.create_call()["state"]["call_id"]
    first.close()
    second = AgentRuntime(Settings(data_dir=tmp_path, prompt_version="router-v2"), clock=lambda: NOW)
    try:
        with pytest.raises(ValueError, match="版本"):
            second.turn(call_id, REQUEST)
    finally:
        second.close()


def test_handoff_ends_automated_actions(runtime):
    call_id = runtime.create_call()["state"]["call_id"]
    runtime.turn(call_id, "转人工")
    result = runtime.turn(call_id, REQUEST)
    assert result["state"]["status"] == "human_escalation"
    assert runtime.store.outcomes(call_id)["bookings"] == []
    assert len(runtime.store.outcomes(call_id)["transfers"]) == 1


def test_negated_handoff_is_not_executed(runtime):
    call_id = runtime.create_call()["state"]["call_id"]
    result = runtime.turn(call_id, "不要转人工，我要预约洗牙")
    assert result["state"]["route"] == "booking"
    assert runtime.store.outcomes(call_id)["transfers"] == []


def test_uncertain_write_never_mutates_into_a_new_booking(tmp_path):
    instance = AgentRuntime(Settings(data_dir=tmp_path), faults={"create_booking": "timeout_before_commit"}, clock=lambda: NOW)
    try:
        call_id = instance.create_call()["state"]["call_id"]
        instance.turn(call_id, REQUEST)
        instance.turn(call_id, "确认预约")
        result = instance.turn(call_id, "改成 2026-11-04 15:00")
        assert result["state"]["booking"]["date"] == "2026-11-03"
        assert result["state"]["status"] == "unknown"
        assert instance.store.outcomes(call_id)["bookings"] == []
    finally:
        instance.close()


def test_api_validates_inputs_and_keeps_secrets_out(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "do-not-disclose-this-test-key")
    with TestClient(create_app(Settings(data_dir=tmp_path))) as client:
        assert client.get("/").status_code == 200
        health = client.get("/health")
        assert health.json()["telephony"] == "not_connected"
        assert "do-not-disclose" not in health.text
        assert client.post("/api/calls/missing/turns", json={"message": "hi"}).status_code == 404
        call_id = client.post("/api/calls").json()["state"]["call_id"]
        assert client.post(f"/api/calls/{call_id}/turns", json={"message": "  "}).status_code == 422
        assert client.post(f"/api/calls/{call_id}/turns", json={"message": "hi", "booking": {}}).status_code == 422
        result = client.post(f"/api/calls/{call_id}/turns", json={"message": "转人工", "request_id": "one"})
        assert result.status_code == 200
        snapshot = client.get(f"/api/calls/{call_id}").json()
        assert len(snapshot["outcomes"]["transfers"]) == 1
        assert len(snapshot["traces"]) == 1
        assert "orchestrator" in client.get("/api/framework").json()["mermaid"]


def test_model_failure_stays_observable_and_handoff_still_works(tmp_path, monkeypatch):
    from langchain_core.runnables import RunnableLambda
    import voice_agent.graph as graph_module

    def unavailable(_):
        raise TimeoutError("provider unavailable")

    monkeypatch.setattr(graph_module, "make_router", lambda _: RunnableLambda(unavailable))
    instance = AgentRuntime(Settings(data_dir=tmp_path), clock=lambda: NOW)
    try:
        call_id = instance.create_call()["state"]["call_id"]
        failed = instance.turn(call_id, REQUEST)
        assert failed["state"]["status"] == "model_error"
        assert instance.store.outcomes(call_id)["bookings"] == []
        transferred = instance.turn(call_id, "转人工")
        assert transferred["state"]["status"] == "human_escalation"
        assert len(instance.store.traces(call_id)) == 2
    finally:
        instance.close()


def test_eval_harness_reports_backend_checked_regressions(tmp_path):
    from evals.runner import run_dataset

    report = tmp_path / "report.json"
    summary = run_dataset("evals/dataset.json", report)
    assert summary["failed"] == 0
    data = json.loads(report.read_text())
    assert data["mode"] == "mock"
    assert data["dataset_version"] == "clinic-local-sql-rag-v3"
    assert any(c["name"] == "confirmed_booking_matches_database"
               for r in data["results"] for t in r["turns"] for c in t["checks"])


def test_claude_sdk_structured_output_drives_real_graph_without_network(tmp_path, monkeypatch):
    """Exercise the installed Claude SDK and structured parser with a fake HTTP response."""
    import httpx2
    from langsmith import tracing_context

    monkeypatch.setenv("ANTHROPIC_API_KEY", "synthetic-test-key")
    monkeypatch.setenv("ANTHROPIC_WORKSPACE_ID", "wrkspc_synthetic")
    captured = []

    def synthetic_send(client, request, *args, **kwargs):
        assert request.headers["anthropic-workspace-id"] == "wrkspc_synthetic"
        body = json.loads(request.content)
        captured.append(body)
        return httpx2.Response(200, request=request, json={
            "id": "msg_synthetic", "type": "message", "role": "assistant",
            "model": "claude-sonnet-4-6", "stop_reason": "tool_use", "stop_sequence": None,
            "content": [{"type": "tool_use", "id": "toolu_synthetic", "name": "TurnDecision", "input": {
                "intent": "booking", "patient_name": "小林", "customer_phone": "2025550100", "service": "cleaning",
                "date": "2026-11-03", "time": "14:00"
            }}],
            "usage": {"input_tokens": 30, "output_tokens": 40},
        })

    monkeypatch.setattr(httpx2.Client, "send", synthetic_send)
    with tracing_context(enabled=False):
        instance = AgentRuntime(Settings(mode="claude", architecture="workflow", data_dir=tmp_path), clock=lambda: NOW)
        try:
            call_id = instance.create_call()["state"]["call_id"]
            result = instance.turn(call_id, REQUEST)
            assert result["state"]["status"] == "awaiting_confirmation"
            assert instance.store.outcomes(call_id)["bookings"] == []
            assert captured[0]["tools"][0]["name"] == "TurnDecision"
            assert "synthetic-test-key" not in json.dumps(result, ensure_ascii=False)
        finally:
            instance.close()


def test_unresolved_write_stays_protected_after_original_date_passes(tmp_path):
    now = [NOW]
    instance = AgentRuntime(Settings(data_dir=tmp_path), faults={"create_booking": "timeout_before_commit"}, clock=lambda: now[0])
    try:
        call_id = instance.create_call()["state"]["call_id"]
        instance.turn(call_id, REQUEST)
        instance.turn(call_id, "确认预约")
        now[0] = datetime.fromisoformat("2026-11-05T09:00:00-08:00")
        result = instance.turn(call_id, "改成 2026-11-06 15:00")
        assert result["state"]["status"] == "unknown"
        assert result["state"]["booking"]["date"] == "2026-11-03"
        assert instance.store.outcomes(call_id)["bookings"] == []
    finally:
        instance.close()


def test_backend_commit_before_checkpoint_is_reconciled(runtime):
    from hashlib import sha256

    call_id = runtime.create_call()["state"]["call_id"]
    response = runtime.turn(call_id, REQUEST)
    candidate = dict(response["state"]["booking"]["offer"])
    candidate.pop("action")
    key = sha256(json.dumps({"call_id": call_id, "operation_id": response["state"]["booking"]["operation_id"], **response["state"]["booking"]["offer"]}, sort_keys=True).encode()).hexdigest()
    # Simulate backend commit followed by a crash before graph state was updated.
    committed = runtime.backend.create_booking(call_id=call_id, idempotency_key=key, **candidate)
    assert committed["status"] == "confirmed"
    resumed = runtime.turn(call_id, "确认预约")
    assert resumed["state"]["booking"]["id"] == committed["id"]
    assert len(runtime.store.outcomes(call_id)["bookings"]) == 1
