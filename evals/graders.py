GRADER_VERSION = "assertions-v2-mcp"


def grade_turn(response: dict, outcomes: dict, expected: dict):
    """Use backend records as evidence, not just agent claims."""
    allowed = {"route", "status", "booking_count", "booking_date", "booking_status", "active_booking_count", "insurance_status", "reply_contains", "tool_absent", "tool_present", "handoff_status", "delegation_order"}
    unknown = set(expected) - allowed
    if unknown:
        raise ValueError(f"Unknown expectation keys: {sorted(unknown)}")
    state = response["state"]
    tools = [e["node"] for e in state.get("events", []) if e.get("type") in {"tool", "mcp_tool"}]
    checks = []

    def check(name, passed, actual):
        checks.append({"name": name, "passed": bool(passed), "actual": actual})

    if "delegation_order" in expected:
        actual = [e["agent"] for e in state.get("events", []) if e.get("type") == "delegation"]
        check("delegation_order", actual == expected["delegation_order"], actual)
    for key in ("route", "status"):
        if key in expected:
            check(key, state.get(key) == expected[key], state.get(key))
    if "booking_count" in expected:
        check("booking_count", len(outcomes["bookings"]) == expected["booking_count"], len(outcomes["bookings"]))
    if "booking_date" in expected:
        actual = [b["date"] for b in outcomes["bookings"]]
        check("booking_date", actual == [expected["booking_date"]], actual)
    if "booking_status" in expected:
        actual = [b["status"] for b in outcomes["bookings"]]
        check("booking_status", actual == [expected["booking_status"]], actual)
    if "active_booking_count" in expected:
        actual = sum(b["status"] in {"confirmed", "modified"} for b in outcomes["bookings"])
        check("active_booking_count", actual == expected["active_booking_count"], actual)
    if "insurance_status" in expected:
        actual = state.get("insurance", {}).get("status")
        check("insurance_status", actual == expected["insurance_status"], actual)
    if "handoff_status" in expected:
        actual = state.get("handoff", {}).get("status")
        check("handoff_status", actual == expected["handoff_status"], actual)
    for text in expected.get("reply_contains", []):
        check(f"reply_contains:{text}", text in state["reply"], state["reply"])
    for name in expected.get("tool_absent", []):
        check(f"tool_absent:{name}", name not in tools, tools)
    for name in expected.get("tool_present", []):
        check(f"tool_present:{name}", name in tools, tools)
    booking = state.get("booking", {})
    if booking.get("status") in {"confirmed", "modified", "cancel"} and booking.get("id"):
        matching = [b for b in outcomes["bookings"] if b["id"] == booking.get("id")]
        check("confirmed_booking_matches_database", len(matching) == 1 and all(
            matching[0][key] == booking.get(key) for key in ("patient_name", "service", "date", "time", "status")
        ), matching)
    return checks
