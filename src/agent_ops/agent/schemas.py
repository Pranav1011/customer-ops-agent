"""Structured outputs the agent produces. Validated everywhere for reliability."""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class Intent(str, Enum):
    order_status = "order_status"
    refund = "refund"
    cancel_subscription = "cancel_subscription"
    address_change = "address_change"
    double_charge = "double_charge"
    damaged_item = "damaged_item"
    unknown = "unknown"


class IntentResult(BaseModel):
    intent: Intent
    confidence: float = Field(ge=0.0, le=1.0)
    rationale: str = ""


class PlanStep(BaseModel):
    description: str
    tool: str | None = None
    expected_outcome: str = ""


class Plan(BaseModel):
    intent: Intent
    summary: str
    steps: list[PlanStep] = Field(default_factory=list)
    risk_level: str = "low"  # low | medium | high
    requires_identity: bool = False


class DecisionAction(str, Enum):
    call_tool = "call_tool"
    finish = "finish"
    escalate = "escalate"


class Decision(BaseModel):
    action: DecisionAction
    tool: str | None = None
    args: dict[str, Any] = Field(default_factory=dict)
    rationale: str = ""
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)


class Resolution(BaseModel):
    status: str  # resolved | escalated | failed
    customer_reply: str
    actions_taken: list[str] = Field(default_factory=list)
    escalation_reason: str | None = None
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
