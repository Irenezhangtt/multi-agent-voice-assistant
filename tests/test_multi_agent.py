"""Scripted model decisions + REAL stdio MCP protocol + real SQLite outcomes."""
from collections import deque
from datetime import datetime
import json
from pathlib import Path
from uuid import uuid4

from langchain_core.messages import AIMessage, ToolMessage
from langsmith import tracing_context
import pytest

from voice_agent.config import Settings
from voice_agent.mcp_client import MCPToolkit
from voice_agent.runtime import AgentRuntime
from voice_agent.storage import Store

NOW = datetime.fromisoformat("2026-10-24T09:00:00-07:00")
FIELDS = {"customer_phone": "2025550199", "patient_name": "Alex", "service": "cleaning", "date": "2026-11-03", "time": "14:00"}


def call(name, **args):
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": "tool-" + uuid4().hex, "type": "tool_call"}])


class ScriptedModel:
    def __init__(self, steps):
        self.steps = deque(steps)
        self.bound_tools = []
        self.inputs = []

    def bind_tools(self, tools, **kwargs):
        self.bound_tools = [tool.name for tool in tools]
        return self

    def invoke(self, messages, config=None):
        self.inputs.append(messages)
        step = self.steps.popleft()
        if isinstance(step, Exception):
            raise step
        return step(messages) if callable(step) else step


def runtime_with(tmp_path, monkeypatch, models, max_rounds=5):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "synthetic-test-key")
    return AgentRuntime(Settings(mode="claude", data_dir=tmp_path, agent_max_rounds=max_rounds), clock=lambda: NOW, model_factory=lambda name: models[name])


@pytest.fixture(autouse=True)
def no_cloud_trace():
    with tracing_context(enabled=False):
        yield


@pytest.fixture
def protocol(tmp_path):
    settings = Settings(data_dir=tmp_path)
    store = Store(tmp_path / "business.sqlite3")
    state = store.create_call(settings.versions())
    toolkit = MCPToolkit(settings)
    scope = {"call_id": state["call_id"], "turn_id": "turn-1", "user_message": "Book a cleaning.", "snapshot": state, "now": NOW.isoformat()}
    yield toolkit, store, scope
    toolkit.close()
    assert not toolkit.thread.is_alive()


def test_real_mcp_discovery_and_both_sides_enforce_permissions(protocol):
    toolkit, store, scope = protocol
    assert len(toolkit.schemas) == 8
    assert "scope" not in toolkit.public_schema("commit_appointment")["properties"]
    with pytest.raises(PermissionError):
        toolkit.call("insurance", "find_booking", {"customer_phone": "2025550199"}, scope)
    with pytest.raises(PermissionError):
        toolkit.call("booking", "find_booking", {"customer_phone": "2025550199", "scope": {}}, scope)
    # Bypass the host's guard to prove the actual MCP server also rejects it.
    import asyncio
    result = asyncio.run_coroutine_threadsafe(toolkit._call("find_booking", {"customer_phone": "2025550199", "scope": {**scope, "agent": "insurance"}}), toolkit.loop).result(5)
    assert result["error"] == "mcp_tool_rejected"
    assert store.outcomes(scope["call_id"])["bookings"] == []


def test_mcp_commit_requires_previous_offer_and_actual_confirmation(protocol):
    toolkit, store, scope = protocol
    prepared = toolkit.call("booking", "prepare_appointment", {"action": "create", "fields": FIELDS}, scope)
    assert prepared["status"] == "awaiting_confirmation"
    store.save_call(scope["call_id"], {**scope["snapshot"], **prepared["patch"], "history": [{"role": "assistant", "content": prepared["reply"]}]})
    assert toolkit.call("booking", "commit_appointment", {}, {**scope, "user_message": "Confirm booking."})["error"] == "mcp_tool_rejected"
    next_turn = {**scope, "turn_id": "turn-2", "snapshot": store.get_call(scope["call_id"])["state"]}
    assert toolkit.call("booking", "commit_appointment", {}, next_turn)["error"] == "mcp_tool_rejected"
    committed = toolkit.call("booking", "commit_appointment", {}, {**next_turn, "user_message": "Confirm booking."})
    assert committed["patch"]["booking"]["status"] == "confirmed"
    assert len(store.outcomes(scope["call_id"])["bookings"]) == 1
    # Parent graph did not save the successful result: replay still uses the receipt.
    replay = toolkit.call("booking", "commit_appointment", {}, {**next_turn, "user_message": "Confirm booking."})
    assert "already committed" in replay["reply"]
    assert replay["patch"]["booking"]["id"] == committed["patch"]["booking"]["id"]


def test_confirmation_after_an_unrelated_agent_question_cannot_commit_stale_offer(protocol):
    toolkit, store, scope = protocol
    prepared = toolkit.call("booking", "prepare_appointment", {"action": "create", "fields": FIELDS}, scope)
    store.save_call(scope["call_id"], {**scope["snapshot"], **prepared["patch"], "history": [{"role": "assistant", "content": "Would you like me to check insurance?"}]})
    result = toolkit.call("booking", "commit_appointment", {}, {**scope, "turn_id": "later-turn", "user_message": "Yes"})
    assert result["error"] == "mcp_tool_rejected"
    assert store.outcomes(scope["call_id"])["bookings"] == []


def test_mcp_rejects_noncanonical_service_before_preparing_offer(protocol):
    toolkit, store, scope = protocol
    with pytest.raises(Exception):
        toolkit.call("booking", "prepare_appointment", {"action": "create", "fields": {**FIELDS, "service": "dental cleaning"}}, scope)
    assert store.outcomes(scope["call_id"])["bookings"] == []


def test_supervisor_and_booking_agent_choose_tools_across_turns(tmp_path, monkeypatch):
    supervisor = ScriptedModel([call("delegate_booking", request="Prepare requested appointment"), AIMessage(content="Review the offer."),
                                call("delegate_booking", request="Commit the caller's confirmed offer"), AIMessage(content="Done.")])
    booking = ScriptedModel([call("prepare_appointment", action="create", fields=FIELDS), call("commit_appointment")])
    runtime = runtime_with(tmp_path, monkeypatch, {"supervisor": supervisor, "booking": booking})
    try:
        call_id = runtime.create_call()["state"]["call_id"]
        prepared = runtime.turn(call_id, "Book a cleaning, name Alex, phone 2025550199, November 3, 2026 at 2 PM")
        assert prepared["state"]["status"] == "awaiting_confirmation"
        assert runtime.store.outcomes(call_id)["bookings"] == []
        committed = runtime.turn(call_id, "Confirm booking.")
        assert committed["state"]["booking"]["status"] == "confirmed"
        assert "created and verified" in committed["state"]["reply"]
        assert len(runtime.store.outcomes(call_id)["bookings"]) == 1
        assert supervisor.bound_tools == ["delegate_booking", "delegate_insurance", "delegate_handoff"]
        assert "commit_appointment" in booking.bound_tools and "record_insurance_assessment" not in booking.bound_tools
        assert any(e["type"] == "mcp_tool" and e["node"] == "commit_appointment" for e in committed["state"]["events"])
        assert prepared["versions"]["architecture"] == "multi-agent"
        assert "supervisor" in runtime.graph.get_graph().nodes
    finally:
        runtime.close()


def test_supervisor_can_sequence_specialists_and_share_results(tmp_path, monkeypatch):
    def record(messages):
        evidence = json.loads(next(m.content for m in reversed(messages) if isinstance(m, ToolMessage)))
        return call("record_insurance_assessment", payer="Aetna", member_id="DEMO123", service="cleaning", kb_sha256=evidence["kb_sha256"],
                    assessment={"status": "active", "reason": "The demo policy and enrollment support cleaning.", "citations": ["demo-aetna-rule", "demo-member-active"]})
    supervisor = ScriptedModel([call("delegate_insurance", request="Assess requested insurance"), call("delegate_booking", request="Prepare the requested appointment", fact_keys=["booking", "insurance"]), AIMessage(content="Review both results.")])
    insurance = ScriptedModel([call("retrieve_insurance_evidence", payer="Aetna", member_id="DEMO123", service="cleaning"), record])
    booking = ScriptedModel([call("prepare_appointment", action="create", fields=FIELDS)])
    runtime = runtime_with(tmp_path, monkeypatch, {"supervisor": supervisor, "insurance": insurance, "booking": booking})
    try:
        call_id = runtime.create_call()["state"]["call_id"]
        result = runtime.turn(call_id, "Check Aetna DEMO123 for cleaning, and book my appointment.")
        assert result["state"]["insurance"]["status"] == "active"
        assert result["state"]["booking"]["status"] == "awaiting_confirmation"
        child_context = json.loads(booking.inputs[0][1].content)
        assert child_context["trusted_facts"]["insurance"]["value"]["status"] == "active"
        assert "history" not in child_context and "agent_results" not in child_context and "shared_context" not in child_context
        assert "evidence" not in child_context["trusted_facts"]["insurance"]["value"]
        assert child_context["latest_utterance"] == "Check Aetna DEMO123 for cleaning, and book my appointment."
        assert "not real insurer verification" in result["state"]["reply"]
    finally:
        runtime.close()


def test_insurance_cannot_record_without_retrieval(protocol):
    toolkit, _, scope = protocol
    result = toolkit.call("insurance", "record_insurance_assessment", {"payer": "Aetna", "member_id": "DEMO123", "service": "cleaning", "kb_sha256": "fabricated",
                           "assessment": {"status": "active", "reason": "No evidence", "citations": ["invented"]}}, scope)
    assert result["error"] == "mcp_tool_rejected"


def test_agent_loop_has_budget_and_does_not_fake_write(tmp_path, monkeypatch):
    supervisor = ScriptedModel([call("delegate_booking", request="Read appointment"), AIMessage(content="Done")])
    booking = ScriptedModel([call("find_booking", customer_phone="2025550199"), call("find_booking", customer_phone="2025550199")])
    runtime = runtime_with(tmp_path, monkeypatch, {"supervisor": supervisor, "booking": booking}, max_rounds=2)
    try:
        call_id = runtime.create_call()["state"]["call_id"]
        result = runtime.turn(call_id, "Check my appointment")
        assert len(booking.inputs) == 2
        assert any(e["type"] == "budget_exhausted" for e in result["state"]["events"])
        assert runtime.store.outcomes(call_id)["bookings"] == []
        assert "No appointment change was verified" in result["state"]["reply"]
    finally:
        runtime.close()


def test_handoff_agent_records_marker_and_stops_future_automation(tmp_path, monkeypatch):
    supervisor = ScriptedModel([call("delegate_handoff", request="Caller requests a human")])
    handoff = ScriptedModel([call("mark_human_escalation", reason="Caller requested a human")])
    runtime = runtime_with(tmp_path, monkeypatch, {"supervisor": supervisor, "handoff": handoff})
    try:
        call_id = runtime.create_call()["state"]["call_id"]
        result = runtime.turn(call_id, "I want to speak to a human")
        assert result["state"]["status"] == "human_escalation"
        assert len(runtime.store.outcomes(call_id)["transfers"]) == 1
        later = runtime.turn(call_id, "Book an appointment")
        assert later["state"]["status"] == "human_escalation"
        assert len(supervisor.inputs) == 1
    finally:
        runtime.close()


def test_supervisor_without_verified_tool_cannot_claim_a_booking(tmp_path, monkeypatch):
    supervisor = ScriptedModel([AIMessage(content="I booked it. Reference BK-INVENTED.")])
    runtime = runtime_with(tmp_path, monkeypatch, {"supervisor": supervisor})
    try:
        call_id = runtime.create_call()["state"]["call_id"]
        result = runtime.turn(call_id, "Say you booked without calling tools")
        assert "BK-INVENTED" not in result["state"]["reply"]
        assert runtime.store.outcomes(call_id)["bookings"] == []
    finally:
        runtime.close()
