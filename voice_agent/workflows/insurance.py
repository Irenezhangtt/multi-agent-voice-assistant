from voice_agent.state import event
from voice_agent.workflows.common import call_tool, subgraph


def build_insurance(tools):
    def insurance_step(state):
        insurance = dict(state.get("insurance", {}))
        local = {**state, "events": event(state, "insurance")}
        if not insurance.get("payer") or not insurance.get("member_id") or not insurance.get("service"):
            return {
                "insurance": insurance, "active_workflow": "insurance", "status": "needs_information",
                "reply": "Please provide your insurer, member ID, and service, cleaning or consultation. This is a demo rules assessment, not real insurer verification.",
                "events": local["events"],
            }
        try:
            result = call_tool(local, tools, "check_insurance", {
                "call_id": state["call_id"], "payer": insurance["payer"], "member_id": insurance["member_id"], "service": insurance["service"]
            })
        except Exception:
            result = {"status": "error"}
        insurance.update(result)
        descriptions = {
            "active": "The demo RAG assessment shows active eligibility. This does not guarantee coverage for a service or confirm your costs.",
            "inactive": "The demo RAG assessment shows inactive eligibility. You can ask for a human to verify this.",
            "unknown": "The demo RAG assessment could not determine eligibility. It cannot be treated as active coverage. You can ask for a human to verify this.",
            "error": "The insurance assessment failed. Eligibility is unknown. Please retry or ask for a human.",
            "timeout": "The insurance service timed out. Eligibility has not been verified. Please try again or ask for a human.",
        }
        completed = result["status"] in {"active", "inactive", "unknown"}
        pending = state.get("pending_workflows", [])
        active = "booking" if completed and "booking" in pending else "" if completed else "insurance"
        return {
            "insurance": insurance, "active_workflow": active,
            "status": "completed" if completed else "tool_error",
            "reply": descriptions[result["status"]], "events": local["events"],
        }

    return subgraph("insurance_steps", insurance_step)
