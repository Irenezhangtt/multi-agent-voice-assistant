from langgraph.graph import END, START, StateGraph

from voice_agent.router import explicit_confirmation, make_router, mock_decision, router_input
from voice_agent.state import AgentState, event
from voice_agent.workflows.booking import BOOKING_FIELDS, build_booking
from voice_agent.workflows.handoff import build_handoff
from voice_agent.workflows.insurance import build_insurance


def build_graph(settings, tools, checkpointer, clock):
    router = make_router(settings)

    def orchestrate(state):
        if state.get("handoff", {}).get("status") == "human_escalation":
            return {"route": "clarify", "reply": "This call has been marked for human escalation. Please start a new call for another automated task.", "status": "human_escalation", "events": event(state, "orchestrator", route="terminal")}
        direct = mock_decision({"message": state["message"], "today": clock().date().isoformat()})
        try:
            decision = direct if direct.intent == "handoff" else router.invoke(router_input(state, settings, clock()))
        except Exception as exc:
            return {"route": "clarify", "status": "model_error", "reply": "The language model is temporarily unavailable. No new business action was taken. Please retry or ask for a human.", "events": event(state, "orchestrator", status="error", error_type=type(exc).__name__)}
        booking = dict(state.get("booking", {}))
        insurance = dict(state.get("insurance", {}))
        pending = list(state.get("pending_workflows", []))
        action = state.get("booking_action", "create")
        if decision.intent in {"booking", "reschedule", "cancel"} and not explicit_confirmation(state["message"]):
            action = {"booking": "create", "reschedule": "modify", "cancel": "cancel"}[decision.intent]
            if booking.get("status") in {"confirmed", "modified", "cancel"}:
                booking.pop("operation_id", None)
        if booking.get("status") != "unknown":
            changes = {key: getattr(decision, key) for key in BOOKING_FIELDS if getattr(decision, key) is not None}
            if changes.get("customer_phone"):
                from voice_agent.identity import normalize_phone
                try:
                    changes["customer_phone"] = normalize_phone(changes["customer_phone"])
                except ValueError:
                    return {"route": "clarify", "reply": "Please provide a valid phone number including its country code. For US numbers, ten digits are also accepted.", "status": "needs_information", "events": event(state, "orchestrator", status="invalid_phone")}
                if booking.get("customer_phone") and changes["customer_phone"] != booking["customer_phone"]:
                    booking = {}
            changed = any(booking.get(key) != value for key, value in changes.items())
            booking.update(changes)
            if changed:
                booking["status"] = "collecting"
                booking.pop("offer", None)
        updates = {key: getattr(decision, key) for key in ("payer", "member_id") if getattr(decision, key) is not None}
        if decision.insurance_service is not None:
            updates["service"] = decision.insurance_service
        elif decision.service is not None:
            updates["service"] = decision.service
        if any(insurance.get(k) != v for k, v in updates.items()):
            insurance = {k: v for k, v in insurance.items() if k in {"payer", "member_id", "service"}}
        insurance.update(updates)
        route = decision.intent
        if route == "continue":
            route = state.get("active_workflow") or ("booking" if explicit_confirmation(state["message"]) and booking else "clarify")
        if route in {"cancel", "reschedule"}:
            route = "booking"
        if route == "insurance" and (decision.also_booking or state.get("active_workflow") == "booking"):
            if "booking" not in pending:
                pending.append("booking")
        if route == "booking":
            pending = [w for w in pending if w != "booking"]
        return {"decision": decision.model_dump(), "booking": booking, "insurance": insurance,
                "booking_action": action, "pending_workflows": pending, "route": route, "reply": "",
                "active_workflow": state.get("active_workflow", ""), "status": "routing",
                "events": event(state, "orchestrator", route=route)}

    def clarify(state):
        if state.get("reply"):
            return {}
        return {"reply": "I can book, change or cancel an appointment, check demo insurance eligibility, or record a human escalation. What would you like to do?", "status": "needs_information"}

    def finish(state):
        return {"history": [*state.get("history", []), {"role": "user", "content": state["message"]}, {"role": "assistant", "content": state["reply"]}][-40:]}

    graph = StateGraph(AgentState)
    graph.add_node("orchestrator", orchestrate)
    graph.add_node("booking", build_booking(tools, settings, clock))
    graph.add_node("insurance", build_insurance(tools))
    graph.add_node("handoff", build_handoff(tools))
    graph.add_node("clarify", clarify)
    graph.add_node("finish", finish)
    graph.add_edge(START, "orchestrator")
    graph.add_conditional_edges("orchestrator", lambda s: s["route"], {name: name for name in ("booking", "insurance", "handoff", "clarify")})
    graph.add_edge("booking", "finish")
    graph.add_conditional_edges("insurance", lambda s: "booking" if s.get("active_workflow") == "booking" and "booking" in s.get("pending_workflows", []) else "finish")
    graph.add_edge("handoff", "finish")
    graph.add_edge("clarify", "finish")
    graph.add_edge("finish", END)
    return graph.compile(checkpointer=checkpointer, name="clinic_voice_agent")
