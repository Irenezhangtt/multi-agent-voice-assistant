"""Per-agent capabilities, enforced by both the host and local MCP server."""
AGENT_TOOLS = {
    "booking": {"find_booking", "search_availability", "prepare_appointment", "commit_appointment", "reconcile_appointment"},
    "insurance": {"retrieve_insurance_evidence", "record_insurance_assessment"},
    "handoff": {"mark_human_escalation"},
}
RESERVED = {"scope", "call_id", "turn_id", "user_message", "snapshot", "agent", "now"}
