from typing import Any, Literal, TypedDict

from pydantic import BaseModel, ConfigDict, Field


class TurnDecision(BaseModel):
    """Extract only what the caller said this turn; never infer authorization."""

    model_config = ConfigDict(extra="forbid")
    intent: Literal["booking", "insurance", "handoff", "continue", "clarify", "cancel", "reschedule"]
    also_booking: bool = False
    customer_phone: str | None = Field(default=None, max_length=40)
    insurance_service: Literal["cleaning", "consultation"] | None = None
    patient_name: str | None = Field(default=None, max_length=80)
    service: Literal["cleaning", "consultation"] | None = None
    date: str | None = Field(default=None, max_length=10, description="YYYY-MM-DD in clinic timezone")
    time: str | None = Field(default=None, max_length=5, description="HH:MM, 24-hour")
    payer: str | None = Field(default=None, max_length=80)
    member_id: str | None = Field(default=None, max_length=80)


class AgentState(TypedDict, total=False):
    trusted_facts: dict[str, Any]
    agent_results: dict[str, Any]
    call_id: str
    case_id: str
    message: str
    turn_id: str
    route: str
    booking_action: str
    active_workflow: str
    pending_workflows: list[str]
    status: str
    decision: dict[str, Any]
    booking: dict[str, Any]
    insurance: dict[str, Any]
    handoff: dict[str, Any]
    reply: str
    events: list[dict[str, Any]]
    history: list[dict[str, str]]


def event(state: AgentState, node: str, **detail):
    return [*state.get("events", []), {"node": node, **detail}]
