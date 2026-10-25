from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime
import json
import sqlite3

from langchain_core.runnables import RunnableLambda
from langsmith import tracing_context
from fastapi.testclient import TestClient
import pytest

from voice_agent.config import Settings
from voice_agent.api import create_app
from voice_agent.identity import normalize_phone
from voice_agent.insurance_rag import EligibilityAssessment, InsuranceRAG
from voice_agent.runtime import AgentRuntime
from voice_agent.storage import Store

NOW = datetime.fromisoformat("2026-10-24T09:00:00-07:00")
REQUEST = "Book a cleaning, name: Alex, 2026-11-03 14:00, phone: (202) 555-0100"


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "synthetic-test-key")
    with tracing_context(enabled=False):
        instance = AgentRuntime(Settings(data_dir=tmp_path), clock=lambda: NOW)
        yield instance
        instance.close()


def book(runtime, message=REQUEST):
    call = runtime.create_call()["state"]["call_id"]
    offered = runtime.turn(call, message)
    assert offered["state"]["status"] == "awaiting_confirmation", offered
    result = runtime.turn(call, "Confirm booking.")
    assert result["state"]["booking"]["status"] == "confirmed", result
    return call, result["state"]["booking"]


def test_phone_formats_and_rejection():
    assert normalize_phone("(202) 555-0100") == normalize_phone("+1 202 555 0100")
    assert normalize_phone("+44 20 7946 0000") == "+442079460000"
    for invalid in ("Alex", "123", "++12025550100", "000000000000000000"):
        with pytest.raises(ValueError):
            normalize_phone(invalid)


def test_cross_call_dedup_cancel_release_and_create_new(runtime):
    first, original = book(runtime)
    second = runtime.create_call()["state"]["call_id"]
    found = runtime.turn(second, REQUEST.replace("(202) 555-0100", "+1 202 555 0100"))
    assert found["state"]["booking"]["id"] == original["id"]
    assert "No duplicate" in found["state"]["reply"]
    offered = runtime.turn(second, "Cancel my appointment.")
    assert offered["state"]["status"] == "awaiting_confirmation"
    assert runtime.backend.find_booking("2025550100")["status"] == "confirmed"
    cancelled = runtime.turn(second, "Confirm cancellation.")
    assert cancelled["state"]["booking"]["status"] == "cancel"
    assert cancelled["state"]["booking"]["id"] == original["id"]
    assert runtime.backend.find_booking("2025550100")["status"] == "not_found"
    assert "14:00" in runtime.backend.search_availability("2026-11-03", "cleaning")["slots"]
    _, new = book(runtime)
    assert new["id"] != original["id"]
    with runtime.store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM bookings").fetchone()[0] == 2
        assert db.execute("SELECT status FROM bookings WHERE id=?", (original["id"],)).fetchone()[0] == "cancel"


def test_change_updates_same_row_and_preserves_audit(runtime):
    _, original = book(runtime)
    call = runtime.create_call()["state"]["call_id"]
    offered = runtime.turn(call, "Reschedule my appointment, phone: 2025550100, 2026-11-04 15:00")
    assert offered["state"]["status"] == "awaiting_confirmation"
    assert runtime.backend.find_booking("2025550100")["date"] == "2026-11-03"
    changed = runtime.turn(call, "Confirm change.")["state"]["booking"]
    assert changed["id"] == original["id"]
    assert (changed["date"], changed["time"], changed["status"]) == ("2026-11-04", "15:00", "modified")
    with runtime.store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM bookings").fetchone()[0] == 1
        audit = db.execute("SELECT before_json,result FROM booking_mutations WHERE action='modify'").fetchone()
        assert json.loads(audit[0])["date"] == "2026-11-03"
        assert json.loads(audit[1])["date"] == "2026-11-04"


def test_cancel_then_rebook_same_call_is_not_old_receipt(runtime):
    call, original = book(runtime)
    runtime.turn(call, "Cancel my appointment.")
    runtime.turn(call, "Confirm cancellation.")
    offered = runtime.turn(call, REQUEST)
    assert offered["state"]["status"] == "awaiting_confirmation"
    new = runtime.turn(call, "Confirm booking.")["state"]["booking"]
    assert new["status"] == "confirmed" and new["id"] != original["id"]


def test_concurrent_same_customer_has_one_active_row(runtime):
    def create(i):
        return runtime.backend.create_booking(call_id=f"call-{i}", customer_phone="2025550100", patient_name="Alex", service="cleaning", date="2026-11-03", time="14:00", idempotency_key=f"key-{i}")
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(create, range(2)))
    assert {r["status"] for r in results} == {"confirmed", "already_exists"}
    with runtime.store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM bookings").fetchone()[0] == 1


def test_stale_revision_does_not_overwrite_latest_booking(runtime):
    _, original = book(runtime)
    args = dict(call_id="another-call", customer_phone="2025550100", action="modify", patient_name="Alex", service="cleaning", date="2026-11-04", time="15:00", expected_revision=1)
    assert runtime.backend.mutate_booking(**args, idempotency_key="change-1")["status"] == "modified"
    assert runtime.backend.mutate_booking(**{**args, "date": "2026-11-05"}, idempotency_key="change-2")["status"] == "stale"
    assert runtime.backend.find_booking("2025550100")["date"] == "2026-11-04"


def test_old_schema_migration_preserves_rows(tmp_path):
    path = tmp_path / "old.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE bookings(id TEXT PRIMARY KEY,call_id TEXT,idempotency_key TEXT UNIQUE,patient_name TEXT,service TEXT,date TEXT,time TEXT,status TEXT,UNIQUE(date,time))")
        db.execute("INSERT INTO bookings VALUES ('old','call','key','Alex','cleaning','2026-11-03','14:00','confirmed')")
    store = Store(path)
    Store(path)  # Migration is repeatable.
    with store.connect() as db:
        row = db.execute("SELECT * FROM bookings WHERE id='old'").fetchone()
        assert row["customer_id"] == "legacy:old"
        assert row["patient_name"] == "Alex"
        assert db.execute("SELECT COUNT(*) FROM bookings_legacy_v1").fetchone()[0] == 1


def test_rag_makes_one_call_with_only_matching_enrollment(runtime):
    calls = []
    def assess(messages):
        calls.append(messages)
        return EligibilityAssessment(status="active", reason="Demo enrollment and service rule match.", citations=["demo-aetna-rule", "demo-member-active"])
    settings = replace(runtime.settings, mode="claude")
    rag = InsuranceRAG(settings, runtime.store, model=RunnableLambda(assess))
    result = rag.check("call", "Aetna", "DEMO123", "cleaning")
    assert result["status"] == "active" and result["demo_only"]
    assert len(calls) == 1
    payload = json.loads(calls[0][1][1])
    assert {d["id"] for d in payload["evidence"]["documents"]} == {"demo-aetna-rule", "demo-member-active"}
    with runtime.store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM insurance_assessments").fetchone()[0] == 1


@pytest.mark.parametrize("member,citations", [("UNKNOWN", ["demo-aetna-rule"]), ("DEMO123", ["invented-source"])])
def test_unsupported_llm_eligibility_becomes_unknown(runtime, member, citations):
    rag = InsuranceRAG(replace(runtime.settings, mode="claude"), runtime.store,
                       model=RunnableLambda(lambda _: EligibilityAssessment(status="active", reason="Unsupported", citations=citations)))
    assert rag.check("call", "Aetna", member, "cleaning")["status"] == "unknown"


def test_insurance_missing_service_asks_before_llm(runtime):
    call = runtime.create_call()["state"]["call_id"]
    response = runtime.turn(call, "Check insurance: Aetna, member ID: DEMO123")
    assert response["state"]["status"] == "needs_information"
    assert runtime.store.outcomes(call)["insurance_queries"] == []
    followup = runtime.turn(call, "cleaning")
    assert followup["state"]["route"] == "insurance"
    assert followup["state"]["insurance"]["status"] == "active"
    assert not followup["state"]["booking"].get("service")


def test_local_inspection_api_exposes_saved_rows_and_history(runtime):
    _, booking = book(runtime)
    with TestClient(create_app(runtime.settings)) as client:
        rows = client.get("/api/bookings", params={"customer_phone": "(202) 555-0100"}).json()["bookings"]
        assert rows[0]["id"] == booking["id"]
        history = client.get(f"/api/bookings/{booking['id']}/history").json()["history"]
        assert history[0]["action"] == "create"
        assert history[0]["before"] is None
        assert client.get("/api/bookings", params={"customer_phone": "bad"}).status_code == 422


def test_rag_real_sdk_request_includes_workspace_and_evidence(runtime, monkeypatch):
    import httpx2
    monkeypatch.setenv("ANTHROPIC_WORKSPACE_ID", "wrkspc_synthetic")
    captured = []
    def send(client, request, *args, **kwargs):
        assert request.headers["anthropic-workspace-id"] == "wrkspc_synthetic"
        payload = json.loads(request.content)
        captured.append(payload)
        return httpx2.Response(200, request=request, json={
            "id": "msg_demo", "type": "message", "role": "assistant", "model": "claude-sonnet-4-6",
            "stop_reason": "tool_use", "stop_sequence": None,
            "content": [{"type": "tool_use", "id": "toolu_demo", "name": "EligibilityAssessment", "input": {
                "status": "active", "reason": "Demo records support the cleaning service.",
                "citations": ["demo-aetna-rule", "demo-member-active"]}}],
            "usage": {"input_tokens": 100, "output_tokens": 50}})
    monkeypatch.setattr(httpx2.Client, "send", send)
    rag = InsuranceRAG(replace(runtime.settings, mode="claude"), runtime.store)
    assert rag.check("synthetic-call", "Aetna", "DEMO123", "cleaning")["status"] == "active"
    assert len(captured) == 1
    assert "demo-aetna-rule" in json.dumps(captured[0]["messages"])
