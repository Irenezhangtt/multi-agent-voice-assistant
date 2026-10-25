"""Local stdio MCP server. SQL rules live here, model decisions live in agents."""
import argparse
from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path
from typing import Literal

from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, ConfigDict, Field

from voice_agent.config import Settings
from voice_agent.identity import normalize_phone
from voice_agent.insurance_rag import EligibilityAssessment
from voice_agent.mcp_policy import AGENT_TOOLS
from voice_agent.router import explicit_confirmation
from voice_agent.storage import Store
from voice_agent.tools import LocalBackend, make_tools
from voice_agent.workflows.booking import build_booking


class BookingDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")
    customer_phone: str | None = None
    patient_name: str | None = Field(default=None, max_length=80)
    service: Literal["cleaning", "consultation"] | None = None
    date: str | None = None
    time: str | None = None


def create_server(settings):
    server = FastMCP("clinic-toolkit", log_level="WARNING")
    store = Store(settings.data_dir / "business.sqlite3")
    backend = LocalBackend(store, settings)
    preparations, retrievals, assessments = set(), {}, {}

    def authorize(scope, name):
        from voice_agent.execution import assert_live, write_scope
        write_scope.set(scope)
        with store.connect() as db:
            assert_live(db, scope)
        if name not in AGENT_TOOLS.get(scope.get("agent"), set()):
            raise PermissionError("Tool is not allowed for this agent")
        state = store.get_call(scope["call_id"])["state"]
        if state.get("handoff", {}).get("status") == "human_escalation":
            raise PermissionError("Automation has ended for this call")
        if not scope.get("turn_id") or not isinstance(scope.get("user_message"), str):
            raise ValueError("Missing trusted call scope")
        return state

    def booking_result(state, scope):
        clock = lambda: datetime.fromisoformat(scope["now"])
        result = build_booking(make_tools(backend), settings, clock).invoke(state)
        keys = ("booking", "booking_action", "status", "active_workflow", "pending_workflows")
        return {"patch": {k: result[k] for k in keys if k in result}, "reply": result["reply"], "status": result["status"]}

    @server.tool()
    def find_booking(customer_phone: str, scope: dict) -> dict:
        """Find this customer's active SQL appointment by phone; no writes."""
        authorize(scope, "find_booking")
        return backend.find_booking(customer_phone)

    @server.tool()
    def search_availability(date: str, service: Literal["cleaning", "consultation"], scope: dict, customer_phone: str = "") -> dict:
        """Read local appointment availability; no writes."""
        authorize(scope, "search_availability")
        return backend.search_availability(date, service, customer_phone)

    @server.tool()
    def prepare_appointment(action: Literal["create", "modify", "cancel"], fields: BookingDraft, scope: dict) -> dict:
        """Validate and prepare a create/modify/cancel offer. Never commits. Return its exact review summary to the caller."""
        previous = authorize(scope, "prepare_appointment")
        if action not in {"create", "modify", "cancel"}:
            raise ValueError("Invalid action")
        if previous.get("booking", {}).get("status") == "unknown":
            raise ValueError("Reconcile the uncertain operation first")
        snapshot = scope["snapshot"]
        booking = dict(snapshot.get("booking", {}))
        changes = fields.model_dump(exclude_none=True)
        if changes.get("customer_phone"):
            changes["customer_phone"] = normalize_phone(changes["customer_phone"])
            if booking.get("customer_phone") != changes["customer_phone"]:
                booking = {}
        booking.update(changes)
        if booking.get("status") in {"confirmed", "modified", "cancel"}:
            booking.pop("operation_id", None)
        booking["status"] = "collecting"
        booking.pop("offer", None)
        result = booking_result({**snapshot, "call_id": scope["call_id"], "booking": booking,
                                 "message": "prepare only", "booking_action": action, "route": "booking", "reply": ""}, scope)
        if result["status"] == "awaiting_confirmation":
            result["patch"]["booking"]["review_summary"] = result["reply"]
        preparations.add((scope["call_id"], scope["turn_id"]))
        if len(preparations) > 256:
            preparations.clear()
            preparations.add((scope["call_id"], scope["turn_id"]))
        return result

    @server.tool()
    def commit_appointment(scope: dict) -> dict:
        """Commit ONLY the offer saved in the previous turn after actual caller confirmation. No editable appointment arguments."""
        previous = authorize(scope, "commit_appointment")
        if not explicit_confirmation(scope["user_message"]):
            raise PermissionError("The actual caller did not explicitly confirm")
        if (scope["call_id"], scope["turn_id"]) in preparations or scope.get("prepared_this_turn"):
            raise PermissionError("A newly prepared offer must be reviewed in a later turn")
        if previous.get("booking", {}).get("status") != "awaiting_confirmation":
            raise PermissionError("No previous-turn appointment offer is awaiting confirmation")
        booking = previous["booking"]
        last = previous.get("history", [])[-1:]
        if not booking.get("review_summary") or not last or last[0].get("role") != "assistant" or booking["review_summary"] not in last[0].get("content", ""):
            raise PermissionError("The latest assistant turn did not display this appointment offer; review it again")
        key = sha256(json.dumps({"call_id": scope["call_id"], "operation_id": booking["operation_id"], **booking["offer"]}, sort_keys=True).encode()).hexdigest()
        receipt = backend.get_booking(scope["call_id"], key)
        if receipt["status"] in {"confirmed", "modified", "cancel"}:
            # A prior process may have committed before saving the parent graph state.
            verified = {**booking, **receipt}
            verified.pop("offer", None)
            return {"patch": {"booking": verified, "booking_action": previous.get("booking_action", "create"), "status": "completed", "active_workflow": ""},
                    "reply": f"The appointment operation was already committed and is verified. Reference {receipt['id']}, status {receipt['status']}.", "status": "completed"}
        return booking_result({**previous, "message": scope["user_message"], "route": "booking", "reply": ""}, scope)

    @server.tool()
    def reconcile_appointment(scope: dict) -> dict:
        """Read the receipt of a previously uncertain operation; never creates a new operation."""
        previous = authorize(scope, "reconcile_appointment")
        if previous.get("booking", {}).get("status") != "unknown":
            raise ValueError("No uncertain operation needs reconciliation")
        return booking_result({**previous, "message": scope["user_message"], "route": "booking", "reply": ""}, scope)

    def query_key(scope, payer, member_id, service):
        return (scope["call_id"], scope["turn_id"], payer.strip(), member_id.strip(), service)

    @server.tool()
    def retrieve_insurance_evidence(payer: str, member_id: str, service: Literal["cleaning", "consultation"], scope: dict) -> dict:
        """Retrieve matching local policy and enrollment evidence. The Insurance Agent must inspect and assess it."""
        authorize(scope, "retrieve_insurance_evidence")
        query = {"payer": payer.strip(), "member_id": member_id.strip(), "service": service}
        evidence = backend.insurance.retrieve(query)
        if len(retrievals) > 128:
            retrievals.clear()
        retrievals[query_key(scope, payer, member_id, service)] = evidence
        with store.connect() as db:
            db.execute("INSERT OR REPLACE INTO insurance_retrievals VALUES (?,?)", (json.dumps(query_key(scope, payer, member_id, service)), json.dumps(evidence)))
        return {**evidence, "query": query}

    @server.tool()
    def record_insurance_assessment(payer: str, member_id: str, service: Literal["cleaning", "consultation"], kb_sha256: str, assessment: EligibilityAssessment, scope: dict) -> dict:
        """Save YOUR evidence-based assessment after retrieval. Invalid citations/unsupported eligibility become unknown. No LLM runs in this tool."""
        authorize(scope, "record_insurance_assessment")
        key = query_key(scope, payer, member_id, service)
        evidence = retrievals.get(key)
        if evidence is None:
            with store.connect() as db:
                row = db.execute("SELECT payload FROM insurance_retrievals WHERE query_key=?", (json.dumps(key),)).fetchone()
            evidence = json.loads(row[0]) if row else None
        if not evidence or evidence["kb_sha256"] != kb_sha256:
            raise PermissionError("Retrieve matching evidence before recording an assessment")
        query = {"payer": payer.strip(), "member_id": member_id.strip(), "service": service}
        if backend.insurance.retrieve(query)["kb_sha256"] != kb_sha256:
            raise ValueError("Knowledge changed; retrieve again")
        if key not in assessments:
            if len(assessments) > 128:
                assessments.clear()
            assessments[key] = backend.insurance.finalize(scope["call_id"], query, evidence, assessment, scope.get("model", "insurance-agent"))
        result = assessments[key]
        labels = {"active": "supports eligibility", "inactive": "does not support eligibility", "unknown": "cannot determine eligibility"}
        reply = f"The demo evidence {labels[result['status']]}. This is not real insurer verification and does not guarantee payment."
        return {"patch": {"insurance": {**query, **result}, "status": "completed", "active_workflow": ""}, "reply": reply, "status": "completed"}

    @server.tool()
    def mark_human_escalation(reason: str, scope: dict) -> dict:
        """Record human_escalation once with shared context; does not call, notify, or schedule anyone."""
        authorize(scope, "mark_human_escalation")
        snapshot = scope["snapshot"]
        summary = json.dumps({"reason": reason[:1000], "original_message": scope["user_message"], "booking": snapshot.get("booking", {}),
                              "insurance": snapshot.get("insurance", {}), "recent_messages": snapshot.get("history", [])[-6:], "identity_verified": False}, ensure_ascii=False)
        result = backend.transfer_call(scope["call_id"], summary, scope["call_id"] + ":handoff")
        return {"patch": {"handoff": result, "status": "human_escalation", "active_workflow": "handoff", "pending_workflows": []},
                "reply": "Your request has been marked for human escalation and a summary was saved. No real phone transfer or callback was made.", "status": "human_escalation"}

    return server


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--knowledge", type=Path, required=True)
    parser.add_argument("--timezone", default="America/Los_Angeles")
    args = parser.parse_args()
    # No dotenv loading, Claude credentials or LLM inside this local tool server.
    create_server(Settings(mode="mock", data_dir=args.data_dir, insurance_kb_path=args.knowledge, timezone=args.timezone)).run(transport="stdio")


if __name__ == "__main__":
    main()
