import json

from voice_agent.state import event
from voice_agent.workflows.common import call_tool, subgraph


def build_handoff(tools):
    def handoff_step(state):
        local = {**state, "events": event(state, "handoff")}
        booking = state.get("booking", {})
        insurance = state.get("insurance", {})
        summary = json.dumps({
            "reason": state["message"], "booking": booking,
            "insurance_status": insurance.get("status", "not_checked"),
            "recent_messages": state.get("history", [])[-6:],
            "source": "local-demo", "identity_verified": False,
        }, ensure_ascii=False)
        result = call_tool(local, tools, "transfer_call", {
            "call_id": state["call_id"], "summary": summary,
            "idempotency_key": state["call_id"] + ":handoff",
        })
        return {
            "handoff": result, "active_workflow": "handoff", "pending_workflows": [],
            "status": "human_escalation",
            "reply": "Your request has been marked for human escalation. A handoff summary was saved. This does not connect a real phone call or schedule a callback.",
            "events": local["events"],
        }

    return subgraph("handoff_steps", handoff_step)
