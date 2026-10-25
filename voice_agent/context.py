"""Only the supervisor owns conversation history; children receive bounded fact views."""
from copy import deepcopy

FACT_FIELDS = {
    "booking": ("id", "customer_id", "customer_phone", "patient_name", "service", "date", "time", "status", "revision"),
    "insurance": ("query_id", "payer", "service", "status", "citations", "demo_only", "kb_sha256"),
    "handoff": ("id", "status"),
}
DEFAULT_FACTS = {"booking": ["booking"], "insurance": ["insurance"], "handoff": ["booking", "insurance"]}
ALLOWED_FACTS = {"booking": {"booking", "insurance"}, "insurance": {"booking", "insurance"}, "handoff": set(FACT_FIELDS)}


def trusted_facts(context):
    facts = {}
    for domain, fields in FACT_FIELDS.items():
        value = context.get(domain, {})
        verified = value.get("id") if domain != "insurance" else value.get("query_id")
        if verified and (domain == "insurance" or value.get("status") not in {"collecting", "awaiting_confirmation", "unknown"}):
            facts[domain] = {"source": "verified_mcp_receipt", "value": {k: value[k] for k in fields if k in value}}
    facts.update(deepcopy(context.get("trusted_facts", {})))
    return facts


def specialist_context(name, context, request, fact_keys=None):
    keys = DEFAULT_FACTS[name] if fact_keys is None else fact_keys
    if len(keys) > 3 or not set(keys) <= ALLOWED_FACTS[name]:
        raise ValueError("Requested facts are outside this specialist's scope")
    facts = trusted_facts(context)
    domain = context.get(name, {})
    pending = {k: deepcopy(domain[k]) for k in (*FACT_FIELDS[name], "member_id", "offer", "review_summary", "operation_id", "missing_fields") if k in domain}
    # Caller-provided fields are task inputs, not verified identity or SQL facts.
    return {"delegated_task": request[:1200], "latest_utterance": context["message"],
            "trusted_facts": {k: facts[k] for k in keys if k in facts},
            "pending_task": pending, "booking_action": context.get("booking_action") if name == "booking" else None,
            "active_workflow": context.get("active_workflow"), "identity_verified": False}
