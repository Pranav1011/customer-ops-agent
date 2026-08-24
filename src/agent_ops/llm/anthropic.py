"""Real Claude provider. Activated with LLM_PROVIDER=anthropic + an API key.

Implements the same LLMProvider surface as the mock by prompting Claude for
structured JSON (classify / plan / decide) and free text (reply / judge). Not
exercised in the offline default; wired and ready so the swap is one env var.
"""

from __future__ import annotations

import json
import time
from typing import Any

from agent_ops.agent.schemas import Decision, Intent, IntentResult, Plan
from agent_ops.config import get_settings
from agent_ops.llm.base import JudgeResult, LLMProvider
from agent_ops.llm.prompts import load_prompt
from agent_ops.tools.registry import REGISTRY


def _extract_json(text: str) -> dict[str, Any]:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```", 2)[1]
        if text.startswith("json"):
            text = text[4:]
    start, end = text.find("{"), text.rfind("}")
    if start >= 0 and end > start:
        text = text[start : end + 1]
    return json.loads(text)


class AnthropicProvider(LLMProvider):
    def __init__(self) -> None:
        super().__init__()
        import anthropic  # lazy import

        s = get_settings()
        if not s.anthropic_api_key:
            raise RuntimeError("LLM_PROVIDER=anthropic requires ANTHROPIC_API_KEY")
        self._client = anthropic.Anthropic(api_key=s.anthropic_api_key)
        self._settings = s
        self._system = load_prompt("system")

    def _call(self, role: str, system: str, user: str, max_tokens: int = 1024) -> str:
        model = self._settings.model_for_role(role)
        t0 = time.perf_counter()
        resp = self._client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        latency = (time.perf_counter() - t0) * 1000
        self._record_real(role, model, resp.usage.input_tokens, resp.usage.output_tokens, latency)
        return "".join(block.text for block in resp.content if block.type == "text")

    def classify(self, request_text: str) -> IntentResult:
        instr = (
            "Classify the intent of this customer message. Return JSON: "
            '{"intent": "order_status|refund|cancel_subscription|address_change|double_charge|damaged_item|unknown", '
            '"confidence": 0.0-1.0, "rationale": "..."}\n\nMessage:\n' + request_text
        )
        data = _extract_json(self._call("classifier", self._system, instr, max_tokens=256))
        return IntentResult(
            intent=Intent(data.get("intent", "unknown")),
            confidence=float(data.get("confidence", 0.5)),
            rationale=data.get("rationale", ""),
        )

    def plan(self, intent: IntentResult, request_text: str, context: dict[str, Any]) -> Plan:
        user = (
            load_prompt("planning")
            + f"\n\nClassified intent: {intent.intent.value} (confidence {intent.confidence})."
            + f"\n\nCustomer message:\n{request_text}\n\nContext:\n{json.dumps(context, default=str)[:2000]}"
        )
        data = _extract_json(self._call("reasoner", self._system, user, max_tokens=1024))
        data.setdefault("intent", intent.intent.value)
        return Plan.model_validate(data)

    def decide(self, view: dict[str, Any]) -> Decision:
        tools = REGISTRY.schemas_for_llm()
        user = (
            load_prompt("decide")
            + f"\n\nAvailable tools:\n{json.dumps(tools, default=str)[:4000]}"
            + f"\n\nCurrent run state:\n{json.dumps(view, default=str)[:4000]}"
        )
        data = _extract_json(self._call("reasoner", self._system, user, max_tokens=512))
        return Decision.model_validate(data)

    def compose_reply(self, view: dict[str, Any]) -> str:
        user = load_prompt("reply") + f"\n\nRun state:\n{json.dumps(view, default=str)[:4000]}"
        return self._call("reasoner", self._system, user, max_tokens=512).strip()

    def score_reply(self, rubric: str, reply: str) -> JudgeResult:
        user = (
            f"{rubric}\n\nCustomer reply to evaluate:\n{reply}\n\n"
            'Return JSON: {"score": 0.0-1.0, "verdict": "pass|fail", "rationale": "..."}'
        )
        data = _extract_json(self._call("judge", self._system, user, max_tokens=512))
        return JudgeResult(
            score=float(data.get("score", 0.0)),
            verdict=str(data.get("verdict", "fail")),
            rationale=data.get("rationale", ""),
            raw=data,
        )

    def compare(self, rubric: str, reply_a: str, reply_b: str) -> tuple[str, JudgeResult]:
        user = (
            f"{rubric}\n\nTwo candidate customer replies. Which better satisfies the rubric?\n\n"
            f"Reply A:\n{reply_a}\n\nReply B:\n{reply_b}\n\n"
            'Return JSON: {"winner": "A|B", "rationale": "..."}'
        )
        data = _extract_json(self._call("judge", self._system, user, max_tokens=256))
        winner = "A" if str(data.get("winner", "A")).upper().startswith("A") else "B"
        return winner, JudgeResult(
            score=1.0, verdict=winner, rationale=data.get("rationale", ""), raw=data
        )
