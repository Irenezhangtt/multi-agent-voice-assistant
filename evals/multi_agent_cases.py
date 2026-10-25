"""Deterministic model doubles for MCP/Supervisor regressions, never live model evals."""
from collections import deque
import json
from uuid import uuid4

from langchain_core.messages import AIMessage, ToolMessage

FIELDS = {"customer_phone": "2025550199", "patient_name": "Alex", "service": "cleaning", "date": "2026-11-03", "time": "14:00"}


def tool(name, **args):
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": uuid4().hex, "type": "tool_call"}])


class ScriptedModel:
    def __init__(self, steps):
        self.steps = deque(steps)

    def bind_tools(self, tools, **kwargs):
        return self

    def invoke(self, messages, config=None):
        step = self.steps.popleft()
        return step(messages) if callable(step) else step


def record_assessment(messages):
    evidence = json.loads(next(m.content for m in reversed(messages) if isinstance(m, ToolMessage)))
    return tool("record_insurance_assessment", payer="Aetna", member_id="DEMO123", service="cleaning", kb_sha256=evidence["kb_sha256"],
                assessment={"status": "active", "reason": "Demo enrollment and policy support cleaning.", "citations": ["demo-aetna-rule", "demo-member-active"]})


def scripted_models(profile):
    final = lambda: AIMessage(content="Return the verified result.")
    if profile == "mcp-booking":
        supervisor = []
        for _ in range(6):
            supervisor.extend([tool("delegate_booking", request="Handle the caller's requested appointment operation"), final()])
        return {"supervisor": ScriptedModel(supervisor), "booking": ScriptedModel([
            tool("prepare_appointment", action="create", fields=FIELDS), tool("commit_appointment"),
            tool("prepare_appointment", action="modify", fields={**FIELDS, "date": "2026-11-04", "time": "15:00"}), tool("commit_appointment"),
            tool("prepare_appointment", action="cancel", fields={}), tool("commit_appointment")])}
    if profile == "mcp-insurance":
        return {"supervisor": ScriptedModel([tool("delegate_insurance", request="Assess the caller's requested demo insurance"), final()]),
                "insurance": ScriptedModel([tool("retrieve_insurance_evidence", payer="Aetna", member_id="DEMO123", service="cleaning"), record_assessment])}
    if profile == "mcp-shared":
        return {"supervisor": ScriptedModel([tool("delegate_insurance", request="Assess requested insurance"), tool("delegate_booking", request="Prepare requested appointment"), final()]),
                "insurance": ScriptedModel([tool("retrieve_insurance_evidence", payer="Aetna", member_id="DEMO123", service="cleaning"), record_assessment]),
                "booking": ScriptedModel([tool("prepare_appointment", action="create", fields=FIELDS)])}
    if profile == "mcp-handoff":
        return {"supervisor": ScriptedModel([tool("delegate_handoff", request="Record caller's request for human help")]),
                "handoff": ScriptedModel([tool("mark_human_escalation", reason="Caller requested a human")])}
    raise ValueError("Unknown scripted multi-agent profile")
