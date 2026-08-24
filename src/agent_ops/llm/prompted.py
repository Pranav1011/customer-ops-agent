"""Shared base for *real* LLM providers that drive the agent by prompting.

Both the Anthropic (Claude) and Ollama (local) providers reason the same way:
they receive the versioned prompts + tool schemas and return structured JSON
(intent / plan / decision) or free text (reply). Only the transport differs, so
each subclass implements a single `_complete(...)` method. This base adds the
reliability the spec calls for: JSON extraction, a repair-retry on malformed
output, and safe fallbacks (fail toward escalation, never crash the loop).
"""

from __future__ import annotations

import json
from abc import abstractmethod
from typing import Any

from agent_ops.agent.schemas import (
    Decision,
    DecisionAction,
    Intent,
    IntentResult,
    Plan,
    PlanStep,
)
from agent_ops.llm.base import JudgeResult, LLMProvider
from agent_ops.llm.prompts import load_prompt
from agent_ops.tools.registry import REGISTRY


def extract_json(text: str) -> dict[str, Any]:
    t = text.strip()
    if t.startswith("```"):
        t = t.split("```", 2)[1]
        if t.lower().startswith("json"):
            t = t[4:]
    start, end = t.find("{"), t.rfind("}")
    if start >= 0 and end > start:
        t = t[start : end + 1]
    return json.loads(t)


class PromptedProvider(LLMProvider):
    def __init__(self) -> None:
        super().__init__()
        self._system = load_prompt("system")

    @abstractmethod
    def _complete(
        self, role: str, system: str, user: str, *, want_json: bool, max_tokens: int
    ) -> str:
        """Run one completion and record usage. Return the raw text."""

    def _json(self, role: str, user: str, *, max_tokens: int = 900) -> dict[str, Any]:
        """Complete expecting JSON, with a single repair-retry."""
        raw = self._complete(role, self._system, user, want_json=True, max_tokens=max_tokens)
        try:
            return extract_json(raw)
        except Exception:
            repair = (
                user + "\n\nYour previous output was not valid JSON. Return ONLY a single valid "
                "JSON object, no prose, no markdown fences."
            )
            raw = self._complete(role, self._system, repair, want_json=True, max_tokens=max_tokens)
            return extract_json(raw)

    # --- classify ---
    def classify(self, request_text: str) -> IntentResult:
        user = (
            "Classify the customer's intent. Return JSON: "
            '{"intent": "order_status|refund|cancel_subscription|address_change|double_charge|damaged_item|unknown", '
            '"confidence": 0.0-1.0, "rationale": "..."}\n\nCustomer message:\n' + request_text
        )
        try:
            d = self._json("classifier", user, max_tokens=200)
            return IntentResult(
                intent=Intent(str(d.get("intent", "unknown"))),
                confidence=float(d.get("confidence", 0.5)),
                rationale=str(d.get("rationale", "")),
            )
        except Exception:
            return IntentResult(intent=Intent.unknown, confidence=0.3, rationale="parse-fallback")

    # --- plan ---
    def plan(self, intent: IntentResult, request_text: str, context: dict[str, Any]) -> Plan:
        user = (
            load_prompt("planning")
            + f"\n\nClassified intent: {intent.intent.value} (confidence {intent.confidence})."
            + f"\n\nCustomer message:\n{request_text}\n\nContext:\n{json.dumps(context, default=str)[:1500]}"
        )
        try:
            d = self._json("reasoner", user, max_tokens=900)
            d.setdefault("intent", intent.intent.value)
            return Plan.model_validate(d)
        except Exception:
            return Plan(
                intent=intent.intent,
                summary=f"Resolve a {intent.intent.value} request following Aurora policy.",
                steps=[PlanStep(description="Gather facts, then act or escalate per policy.")],
                risk_level="medium",
            )

    # --- decide ---
    def decide(self, view: dict[str, Any]) -> Decision:
        tools = REGISTRY.schemas_for_llm()
        user = (
            load_prompt("decide")
            + f"\n\nAvailable tools (name, description, kind, parameters):\n{json.dumps(tools, default=str)[:5000]}"
            + f"\n\nCurrent run state (intent, plan, scratchpad of prior tool results):\n{json.dumps(view, default=str)[:5000]}"
        )
        try:
            d = self._json("reasoner", user, max_tokens=400)
            return Decision.model_validate(d)
        except Exception:
            return Decision(
                action=DecisionAction.escalate,
                args={"reason": "Could not parse a valid next action from the model."},
                rationale="decision parse-fallback",
                confidence=0.3,
            )

    # --- reply ---
    def compose_reply(self, view: dict[str, Any]) -> str:
        user = load_prompt("reply") + f"\n\nRun state:\n{json.dumps(view, default=str)[:5000]}"
        return self._complete(
            "reasoner", self._system, user, want_json=False, max_tokens=500
        ).strip()

    # --- judge (eval harness) ---
    def score_reply(self, rubric: str, reply: str) -> JudgeResult:
        user = (
            f"{rubric}\n\nCustomer reply to evaluate:\n{reply}\n\n"
            'Return JSON: {"score": 0.0-1.0, "verdict": "pass|fail", "rationale": "..."}'
        )
        try:
            d = self._json("judge", user, max_tokens=300)
            return JudgeResult(
                score=float(d.get("score", 0.0)),
                verdict=str(d.get("verdict", "fail")),
                rationale=str(d.get("rationale", "")),
                raw=d,
            )
        except Exception:
            return JudgeResult(score=0.0, verdict="fail", rationale="judge parse-fallback")

    def compare(self, rubric: str, reply_a: str, reply_b: str) -> tuple[str, JudgeResult]:
        user = (
            f"{rubric}\n\nWhich reply better satisfies the rubric?\n\nReply A:\n{reply_a}\n\nReply B:\n{reply_b}\n\n"
            'Return JSON: {"winner": "A|B", "rationale": "..."}'
        )
        try:
            d = self._json("judge", user, max_tokens=200)
            winner = "A" if str(d.get("winner", "A")).strip().upper().startswith("A") else "B"
            return winner, JudgeResult(
                score=1.0, verdict=winner, rationale=str(d.get("rationale", "")), raw=d
            )
        except Exception:
            return "A", JudgeResult(score=0.0, verdict="A", rationale="compare parse-fallback")
