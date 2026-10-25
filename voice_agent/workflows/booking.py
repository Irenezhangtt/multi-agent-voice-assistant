from datetime import date, datetime
from hashlib import sha256
import json
from zoneinfo import ZoneInfo
from uuid import uuid4

from voice_agent.router import explicit_confirmation
from voice_agent.state import event
from voice_agent.workflows.common import booking_label, call_tool, subgraph

BOOKING_FIELDS = ("customer_phone", "patient_name", "service", "date", "time")
COMMITTED = {"confirmed", "modified", "cancel"}


def build_booking(tools, settings, clock):
    def booking_step(state):
        booking = dict(state.get("booking", {}))
        local = {**state, "events": event(state, "booking")}
        pending = [w for w in state.get("pending_workflows", []) if w != "booking"]
        action = state.get("booking_action", "create")

        def result(reply, status, active="booking"):
            prefix = state.get("reply", "") if state.get("route") == "insurance" else ""
            return {"booking": booking, "booking_action": action, "reply": (prefix + "\n" + reply).strip(), "status": status,
                    "active_workflow": active, "pending_workflows": pending, "events": local["events"]}

        if booking.get("status") == "unknown":
            receipt = call_tool(local, tools, "get_booking", {"call_id": state["call_id"], "idempotency_key": booking["idempotency_key"]})
            if receipt["status"] in COMMITTED:
                booking.update(receipt)
                return result(f"The appointment operation has been verified. Status {receipt['status']}. Reference {receipt['id']}: {booking_label(booking)}.", "completed", "")
            return result("The previous operation is still unconfirmed. I will not submit a new operation. Please ask for a human to verify it.", "unknown")
        if not booking.get("customer_phone"):
            return result("Please provide your phone number, including country code. It is used as your customer ID.", "needs_information")
        existing = call_tool(local, tools, "find_booking", {"customer_phone": booking["customer_phone"]})
        if existing["status"] == "timeout":
            return result("I could not verify your existing appointment. No changes were made. Please retry.", "tool_error")
        present = existing["status"] in {"confirmed", "modified"}
        if not present and action == "cancel":
            booking = {"customer_phone": booking["customer_phone"], "status": "cancel"}
            return result("No active appointment exists for this customer. The pending request is cancelled; no appointment was changed.", "cancelled", "")
        if not present and action == "modify":
            if state.get("active_workflow") == "booking" and all(booking.get(k) for k in BOOKING_FIELDS):
                action = "create"  # Editing an unsubmitted request.
            else:
                return result("No active appointment exists for this customer. Please request a new booking instead.", "needs_information")
        if present:
            for key in BOOKING_FIELDS:
                if key != "customer_phone" and not booking.get(key):
                    booking[key] = existing[key]
            if action == "create":
                differs = any(booking.get(k) != existing.get(k) for k in BOOKING_FIELDS if k != "customer_phone")
                if not differs:
                    booking.update(existing)
                    return result(f"You already have an appointment, {existing['id']}: {booking_label(existing)}. No duplicate was created.", "completed", "")
                action = "modify"
        missing = [f for f in BOOKING_FIELDS if not booking.get(f)]
        if missing:
            labels = {"patient_name": "your name", "service": "the service, cleaning or consultation", "date": "the appointment date", "time": "the appointment time", "customer_phone": "your phone number"}
            return result("Please provide " + ", ".join(labels[f] for f in missing) + ".", "needs_information")
        if action != "cancel":
            try:
                target = date.fromisoformat(booking["date"])
                now = clock().astimezone(ZoneInfo(settings.timezone))
                target_dt = datetime.fromisoformat(f"{booking['date']}T{booking['time']}:00").replace(tzinfo=ZoneInfo(settings.timezone))
                if not 0 <= (target - now.date()).days <= 90 or target_dt <= now:
                    raise ValueError()
            except (ValueError, TypeError):
                booking.pop("offer", None)
                return result("Please provide a valid future date within 90 days and a 24-hour time in the clinic time zone.", "needs_information")
        candidate = {key: booking[key] for key in BOOKING_FIELDS}
        candidate.update(action=action, expected_revision=existing["revision"] if present else 0)
        booking.setdefault("operation_id", str(uuid4()))
        key = sha256(json.dumps({"call_id": state["call_id"], "operation_id": booking["operation_id"], **candidate}, sort_keys=True).encode()).hexdigest()
        approved = explicit_confirmation(state["message"]) and booking.get("status") == "awaiting_confirmation" and booking.get("offer") == candidate
        if approved:
            receipt = call_tool(local, tools, "get_booking", {"call_id": state["call_id"], "idempotency_key": key})
            if receipt["status"] in COMMITTED:
                booking.update(receipt)
                return result(f"Appointment operation verified. Reference {receipt['id']}. Status {receipt['status']}.", "completed", "")
            if receipt["status"] == "timeout":
                booking.update(status="unknown", idempotency_key=key)
                return result("The previous write cannot be verified. I will not submit again. Please ask for a human.", "unknown")
        if action != "cancel":
            available = call_tool(local, tools, "search_availability", {"date": booking["date"], "service": booking["service"], "customer_phone": booking["customer_phone"]})
            if available["status"] != "success":
                booking.pop("offer", None)
                return result("Availability lookup failed. No appointment was changed. Please retry.", "tool_error")
            if booking["time"] not in available["slots"]:
                booking.pop("offer", None)
                return result("That time is unavailable. Available times: " + ", ".join(available["slots"]), "needs_information")
        if not approved:
            booking.update(status="awaiting_confirmation", offer=candidate)
            verb = {"create": "create", "modify": "modify the existing", "cancel": "cancel the existing"}[action]
            return result(f"Please review: {verb} appointment for phone {booking['customer_phone']}, {booking_label(booking)}, time zone {settings.timezone}. Say confirm booking, confirm change, or confirm cancellation to authorize this operation.", "awaiting_confirmation")
        booking["idempotency_key"] = key
        args = {"call_id": state["call_id"], **candidate, "idempotency_key": key}
        tool_name = "create_booking" if action == "create" else "mutate_booking"
        if action == "create":
            args.pop("action")
        changed = call_tool(local, tools, tool_name, args)
        receipt = call_tool(local, tools, "get_booking", {"call_id": state["call_id"], "idempotency_key": key})
        if receipt["status"] in COMMITTED:
            booking.update(receipt)
            booking.pop("offer", None)
            return result(f"Your appointment was { {'confirmed':'created','modified':'modified','cancel':'cancelled'}[receipt['status']] } and verified in the local database. Reference {receipt['id']}: {booking_label(booking)}.", "completed", "")
        if changed["status"] in {"conflict", "stale", "already_exists", "not_found"}:
            booking.pop("offer", None)
            booking["status"] = "collecting"
            return result("The appointment changed or the slot is unavailable. No requested change was committed. Please review the latest details before confirming again.", "needs_information")
        booking["status"] = "unknown"
        return result("The operation result is unconfirmed. I will not claim success or submit again. Please ask for a human to verify it.", "unknown")

    return subgraph("booking_steps", booking_step)
