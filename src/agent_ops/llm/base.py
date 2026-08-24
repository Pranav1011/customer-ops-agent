"""LLM provider interface + usage/cost accounting shared by all providers.

The agent talks to the model through this small, explicit surface (classify,
plan, decide, compose_reply, judge) rather than raw chat. That keeps the mock
deterministic, keeps the real-Claude swap a one-liner, and makes every model
call individually traceable with simulated-or-real token + cost accounting.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from agent_ops.agent.schemas import Decision, IntentResult, Plan

# Approximate Claude pricing (USD per 1M tokens) — used for the efficiency
# metric. With the mock provider these are applied to *estimated* token counts
# so cost/latency numbers are still meaningful offline.
PRICES: dict[str, tuple[float, float]] = {
    "haiku": (1.0, 5.0),
    "sonnet": (3.0, 15.0),
    "opus": (15.0, 75.0),
}


def price_for(model: str) -> tuple[float, float]:
    for key, price in PRICES.items():
        if key in model:
            return price
    return PRICES["sonnet"]


def estimate_tokens(text: str) -> int:
    """Rough token estimate (~4 chars/token)."""
    return max(1, len(text) // 4)


@dataclass
class UsageEvent:
    role: str
    model: str
    tokens_in: int
    tokens_out: int
    cost_usd: float
    latency_ms: float


@dataclass
class JudgeResult:
    score: float  # 0..1
    verdict: str  # e.g. pass | fail
    rationale: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


class LLMProvider(ABC):
    """Everything the agent needs from a model."""

    def __init__(self) -> None:
        self._events: list[UsageEvent] = []

    # --- usage accounting ---
    def _record(
        self, role: str, model: str, prompt: str, output: str, latency_ms: float = 0.0
    ) -> None:
        tin = estimate_tokens(prompt)
        tout = estimate_tokens(output)
        pin, pout = price_for(model)
        cost = (tin * pin + tout * pout) / 1_000_000
        self._events.append(
            UsageEvent(
                role=role,
                model=model,
                tokens_in=tin,
                tokens_out=tout,
                cost_usd=cost,
                latency_ms=latency_ms,
            )
        )

    def _record_real(
        self, role: str, model: str, tokens_in: int, tokens_out: int, latency_ms: float
    ) -> None:
        pin, pout = price_for(model)
        cost = (tokens_in * pin + tokens_out * pout) / 1_000_000
        self._events.append(
            UsageEvent(
                role=role,
                model=model,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                cost_usd=cost,
                latency_ms=latency_ms,
            )
        )

    def drain_usage(self) -> list[UsageEvent]:
        """Return usage events since the last drain and clear them."""
        events, self._events = self._events, []
        return events

    # --- model surface ---
    @abstractmethod
    def classify(self, request_text: str) -> IntentResult: ...

    @abstractmethod
    def plan(self, intent: IntentResult, request_text: str, context: dict[str, Any]) -> Plan: ...

    @abstractmethod
    def decide(self, view: dict[str, Any]) -> Decision:
        """Given the current run view (intent, plan, scratchpad of tool
        results, iteration count), decide the next action."""

    @abstractmethod
    def compose_reply(self, view: dict[str, Any]) -> str: ...

    @abstractmethod
    def judge(self, rubric: str, content: str, options: list[str]) -> JudgeResult:
        """LLM-as-judge for the eval harness (Phase 4)."""
