"""Deterministic mock provider.

Simulates the agent's reasoning with rule-based, per-intent strategies so the
*entire* system (tool loop, guardrails, memory, tracing, evals, API) runs and is
testable offline with zero cost and perfect reproducibility. Swapping to real
Claude reasoning (llm/anthropic.py) changes nothing else.

The strategies here are intentionally faithful to Aurora policy: they call the
same real tools, respect the same guardrail signals (identity, thresholds,
escalation), and ground every reply in tool results.
"""

from __future__ import annotations

from typing import Any

from agent_ops.agent.schemas import (
    Decision,
    DecisionAction,
    Intent,
    IntentResult,
    Plan,
    PlanStep,
)
from agent_ops.config import get_settings
from agent_ops.llm.base import JudgeResult, LLMProvider

# Keyword rules for intent classification (checked in priority order).
_INTENT_RULES: list[tuple[Intent, tuple[str, ...]]] = [
    (
        Intent.double_charge,
        ("charged twice", "double charge", "duplicate charge", "charged me twice", "two charges"),
    ),
    (Intent.damaged_item, ("damaged", "broken", "defective", "torn", "cracked", "arrived damaged")),
    (
        Intent.cancel_subscription,
        (
            "cancel my subscription",
            "cancel subscription",
            "cancel my membership",
            "stop my subscription",
            "unsubscribe",
        ),
    ),
    (
        Intent.address_change,
        (
            "change my address",
            "change shipping",
            "update address",
            "wrong address",
            "ship to my new",
            "moved",
        ),
    ),
    (Intent.refund, ("refund", "money back", "reimburse", "want my money")),
    (
        Intent.order_status,
        (
            "where is my order",
            "where's my order",
            "track",
            "tracking",
            "hasn't arrived",
            "has not arrived",
            "when will",
            "shipped",
            "delivery status",
        ),
    ),
]


def _find_intent(text: str) -> tuple[Intent, float]:
    low = text.lower()
    for intent, kws in _INTENT_RULES:
        for kw in kws:
            if kw in low:
                return intent, 0.9
    # Weak single-word fallbacks.
    if "order" in low:
        return Intent.order_status, 0.55
    return Intent.unknown, 0.3


class MockProvider(LLMProvider):
    def __init__(self) -> None:
        super().__init__()
        s = get_settings()
        self._models = {
            "classifier": s.model_classifier,
            "reasoner": s.model_reasoner,
            "judge": s.model_judge,
        }

    # --- helpers ---
    def _scratch(self, view: dict[str, Any], tool: str) -> list[dict[str, Any]]:
        return [s for s in view.get("scratchpad", []) if s.get("tool") == tool]

    def _last_ok(self, view: dict[str, Any], tool: str) -> dict[str, Any] | None:
        for s in reversed(self._scratch(view, tool)):
            res = s.get("result", {})
            if res.get("ok"):
                return res.get("data", {})
        return None

    def _sim(self, role: str, prompt: str, output: str) -> None:
        model = self._models.get(role, self._models["reasoner"])
        # Simulated latency: base + proportional to output length.
        latency = 40.0 + len(output) * 0.15
        self._record(role, model, prompt, output, latency_ms=latency)

    # --- classify ---
    def classify(self, request_text: str) -> IntentResult:
        intent, conf = _find_intent(request_text)
        out = f"{intent.value}:{conf}"
        self._sim("classifier", request_text, out)
        return IntentResult(intent=intent, confidence=conf, rationale="keyword match")

    # --- plan ---
    def plan(self, intent: IntentResult, request_text: str, context: dict[str, Any]) -> Plan:
        it = intent.intent
        steps: list[PlanStep]
        risk = "low"
        requires_identity = False

        if it == Intent.order_status:
            steps = [
                PlanStep(
                    description="Locate the order",
                    tool="get_order",
                    expected_outcome="order record",
                ),
                PlanStep(
                    description="Summarize status and tracking for the customer",
                    tool=None,
                    expected_outcome="grounded reply",
                ),
            ]
        elif it == Intent.refund:
            risk = "medium"
            steps = [
                PlanStep(description="Verify the order and amount paid", tool="get_order"),
                PlanStep(
                    description="Check refund policy and threshold", tool="search_knowledge_base"
                ),
                PlanStep(
                    description="Issue refund if within policy, else escalate", tool="issue_refund"
                ),
            ]
        elif it == Intent.double_charge:
            risk = "medium"
            steps = [
                PlanStep(
                    description="Inspect payment history for a duplicate",
                    tool="get_payment_history",
                ),
                PlanStep(description="Refund the duplicate payment", tool="issue_refund"),
            ]
        elif it == Intent.damaged_item:
            risk = "medium"
            steps = [
                PlanStep(description="Verify the order", tool="get_order"),
                PlanStep(
                    description="Apply damaged-item policy (refund or replacement)",
                    tool="issue_refund",
                ),
            ]
        elif it == Intent.address_change:
            risk = "high"
            requires_identity = True
            steps = [
                PlanStep(description="Verify identity", tool="get_customer"),
                PlanStep(
                    description="Update shipping address if order not yet shipped",
                    tool="update_shipping_address",
                ),
            ]
        elif it == Intent.cancel_subscription:
            risk = "high"
            steps = [
                PlanStep(description="Look up the subscription", tool="get_subscription"),
                PlanStep(
                    description="Escalate cancellation for human confirmation",
                    tool="escalate_to_human",
                ),
            ]
        else:
            steps = [
                PlanStep(
                    description="Clarify or escalate ambiguous request", tool="escalate_to_human"
                )
            ]
            risk = "medium"

        summary = f"Resolve a {it.value} request following Aurora policy."
        prompt = request_text + str(context)
        self._sim("reasoner", prompt, summary + str(steps))
        return Plan(
            intent=it,
            summary=summary,
            steps=steps,
            risk_level=risk,
            requires_identity=requires_identity,
        )

    # --- decide (the tool loop brain) ---
    def decide(self, view: dict[str, Any]) -> Decision:
        intent = view.get("intent")
        handler = {
            Intent.order_status.value: self._decide_order_status,
            Intent.refund.value: self._decide_refund,
            Intent.damaged_item.value: self._decide_refund,  # damaged -> full refund path
            Intent.double_charge.value: self._decide_double_charge,
            Intent.address_change.value: self._decide_address,
            Intent.cancel_subscription.value: self._decide_cancel,
        }.get(intent, self._decide_unimplemented)
        decision = handler(view)
        self._sim("reasoner", str(view)[:2000], decision.model_dump_json())
        return decision

    def _decide_order_status(self, view: dict[str, Any]) -> Decision:
        order_id = view.get("order_id")
        customer_id = view.get("customer_id")

        got_order = self._last_ok(view, "get_order")
        tried_order = bool(self._scratch(view, "get_order"))
        tried_find = bool(self._scratch(view, "find_orders"))

        if order_id and not tried_order:
            return Decision(
                action=DecisionAction.call_tool,
                tool="get_order",
                args={"order_id": order_id},
                rationale="Fetch the referenced order to ground the status.",
            )
        if got_order:
            return Decision(
                action=DecisionAction.finish, rationale="Order located; summarize status."
            )
        # Order not referenced or not found — fall back to the customer's orders.
        if customer_id and not tried_find:
            return Decision(
                action=DecisionAction.call_tool,
                tool="find_orders",
                args={"customer_id": customer_id, "limit": 5},
                rationale="No valid order id; list the customer's recent orders.",
            )
        return Decision(
            action=DecisionAction.finish, rationale="Best-effort summary from available data."
        )

    def _decide_refund(self, view: dict[str, Any]) -> Decision:
        """Refund / damaged-item path: verify the order, then refund the
        refundable remaining. Policy gates the amount (auto vs escalate)."""
        order_id = view.get("order_id")
        reason = (
            "damaged item"
            if view.get("intent") == Intent.damaged_item.value
            else "customer refund request"
        )

        if not order_id:
            return Decision(
                action=DecisionAction.escalate,
                args={"reason": "No order id provided; cannot verify what to refund."},
                rationale="Ambiguous refund — need an order id.",
            )
        if not self._scratch(view, "get_order"):
            return Decision(
                action=DecisionAction.call_tool,
                tool="get_order",
                args={"order_id": order_id},
                rationale="Verify the order and amount paid before refunding.",
            )
        order = self._last_ok(view, "get_order")
        if not order:
            return Decision(
                action=DecisionAction.escalate,
                args={"reason": f"Could not locate order {order_id} to refund."},
                rationale="Order not found — escalate.",
            )
        if not self._scratch(view, "issue_refund"):
            remaining = round(
                float(order["total"]) - float(order.get("refunded_amount", 0) or 0), 2
            )
            return Decision(
                action=DecisionAction.call_tool,
                tool="issue_refund",
                args={"order_id": order_id, "amount": remaining, "reason": reason},
                rationale="Issue a refund for the refundable remaining amount.",
            )
        if self._last_ok(view, "issue_refund"):
            return Decision(
                action=DecisionAction.finish, rationale="Refund issued; confirm to the customer."
            )
        return Decision(
            action=DecisionAction.escalate,
            args={"reason": "Refund could not be completed automatically."},
            rationale="Refund attempt failed or was blocked — escalate.",
        )

    def _decide_double_charge(self, view: dict[str, Any]) -> Decision:
        customer_id = view.get("customer_id")
        order_id = view.get("order_id")
        if not self._scratch(view, "get_payment_history"):
            args = {"customer_id": customer_id}
            if order_id:
                args["order_id"] = order_id
            return Decision(
                action=DecisionAction.call_tool,
                tool="get_payment_history",
                args=args,
                rationale="Inspect payments for a duplicate charge.",
            )
        pays = (self._last_ok(view, "get_payment_history") or {}).get("payments", [])
        dup = next(
            (p for p in pays if p.get("is_duplicate") and p.get("status") == "succeeded"), None
        )
        if dup and not self._scratch(view, "issue_refund"):
            return Decision(
                action=DecisionAction.call_tool,
                tool="issue_refund",
                args={
                    "order_id": dup["order_id"],
                    "amount": dup["amount"],
                    "reason": "duplicate charge refund",
                },
                rationale="Refund the duplicate payment.",
            )
        if self._scratch(view, "issue_refund"):
            if self._last_ok(view, "issue_refund"):
                return Decision(action=DecisionAction.finish, rationale="Duplicate refunded.")
            return Decision(
                action=DecisionAction.escalate,
                args={"reason": "Duplicate-charge refund could not be completed."},
                rationale="Refund failed/blocked — escalate.",
            )
        # No duplicate found — nothing to refund.
        return Decision(action=DecisionAction.finish, rationale="No duplicate charge found.")

    def _decide_address(self, view: dict[str, Any]) -> Decision:
        order_id = view.get("order_id")
        if not order_id:
            return Decision(
                action=DecisionAction.escalate,
                args={"reason": "No order id provided for the address change."},
                rationale="Need an order id.",
            )
        if not self._scratch(view, "update_shipping_address"):
            # Propose the change; the policy engine enforces identity + that the
            # order hasn't shipped. Placeholder address stands in for the parsed one.
            address = {
                "line1": "Updated address per customer request",
                "city": "Springfield",
                "region": "IL",
                "postal_code": "62701",
                "country": "US",
            }
            return Decision(
                action=DecisionAction.call_tool,
                tool="update_shipping_address",
                args={"order_id": order_id, "address": address},
                rationale="Attempt the address change (policy will gate identity/shipment).",
            )
        if self._last_ok(view, "update_shipping_address"):
            return Decision(action=DecisionAction.finish, rationale="Address updated.")
        return Decision(
            action=DecisionAction.escalate,
            args={"reason": "Address change could not be completed (order may have shipped)."},
            rationale="Blocked — escalate.",
        )

    def _decide_cancel(self, view: dict[str, Any]) -> Decision:
        customer_id = view.get("customer_id")
        if not self._scratch(view, "get_subscription"):
            return Decision(
                action=DecisionAction.call_tool,
                tool="get_subscription",
                args={"customer_id": customer_id},
                rationale="Look up the subscription before cancelling.",
            )
        # Propose the cancellation; policy always routes it to a human.
        return Decision(
            action=DecisionAction.call_tool,
            tool="cancel_subscription",
            args={"customer_id": customer_id},
            rationale="Cancellation requested — policy will require human confirmation.",
        )

    def _decide_unimplemented(self, view: dict[str, Any]) -> Decision:
        # Phase 1: non-order intents are safely escalated. Phase 2 implements them.
        return Decision(
            action=DecisionAction.escalate,
            tool="escalate_to_human",
            args={
                "reason": f"Intent '{view.get('intent')}' not yet automated; routing to a human."
            },
            rationale="Unimplemented intent — fail safe by escalating.",
            confidence=0.5,
        )

    # --- compose reply ---
    def compose_reply(self, view: dict[str, Any]) -> str:
        intent = view.get("intent")
        if view.get("escalated"):
            reply = (
                "Thanks for reaching out. I've routed your request to a specialist on our "
                "team who will follow up with you shortly. We appreciate your patience."
            )
        elif intent == Intent.order_status.value:
            reply = self._reply_order_status(view)
        else:
            reply = self._reply_action(view)
        self._sim("reasoner", str(view)[:2000], reply)
        return reply

    def _reply_action(self, view: dict[str, Any]) -> str:
        refund = self._last_ok(view, "issue_refund")
        if refund:
            return (
                f"You're all set — I've issued a refund of ${float(refund['amount']):.2f} to your "
                f"original payment method for order {refund['order_id']}. It typically posts within "
                "5–10 business days. Apologies for the inconvenience, and thanks for your patience."
            )
        addr = self._last_ok(view, "update_shipping_address")
        if addr:
            a = addr["shipping_address"]
            return (
                f"Done — I've updated the shipping address on order {addr['order_id']} to "
                f"{a['line1']}, {a['city']}, {a['region']} {a['postal_code']}. Let me know if there's "
                "anything else I can help with."
            )
        if view.get("intent") == Intent.double_charge.value:
            return (
                "I took a close look at the payments on your account and didn't find a duplicate "
                "charge for that order. If you're seeing something unexpected on your statement, "
                "send me the date and amount and I'll investigate further."
            )
        return (
            "Thanks for contacting Aurora. I've reviewed your request and a member of our team "
            "will follow up with the details shortly."
        )

    def _reply_order_status(self, view: dict[str, Any]) -> str:
        order = self._last_ok(view, "get_order")
        if not order:
            find = self._last_ok(view, "find_orders") or {}
            orders = find.get("orders", [])
            if orders:
                order = orders[0]
        if not order:
            return (
                "I'm sorry — I couldn't locate that order with the details provided. "
                "Could you share the order number (it looks like ORD-000000) so I can track it down?"
            )
        oid = order.get("order_id")
        status = order.get("status")
        carrier = order.get("carrier")
        tracking = order.get("tracking_number")
        if status in ("shipped",) and tracking:
            return (
                f"Good news — your order {oid} has shipped via {carrier} (tracking {tracking}). "
                "Standard delivery is 3–5 business days from the ship date. You can follow the "
                "tracking link for the latest location. Anything else I can help with?"
            )
        if status == "delivered":
            when = (order.get("delivered_at") or "")[:10]
            return (
                f"Your order {oid} shows as delivered{f' on {when}' if when else ''}. If it hasn't "
                "turned up, check with anyone at your address and around the property, and let me "
                "know — I can open a carrier investigation or arrange a reship."
            )
        if status in ("placed", "packed"):
            return (
                f"Your order {oid} is confirmed and currently being prepared ({status}). You'll get "
                "a tracking number by email as soon as it ships, typically within a day or two."
            )
        if status == "cancelled":
            return f"Order {oid} shows as cancelled. If that's unexpected, let me know and I'll look into it right away."
        return f"Your order {oid} is currently in '{status}' status. Let me know if you'd like more detail."

    # --- judge (Phase 4) ---
    def judge(self, rubric: str, content: str, options: list[str]) -> JudgeResult:
        # Deterministic heuristic judge for offline runs: rewards grounded,
        # non-empty, on-policy replies. The real study (position bias, etc.)
        # activates with the Anthropic provider.
        text = content.lower()
        score = 0.5
        if len(content) > 40:
            score += 0.2
        if any(
            tok in text
            for tok in (
                "order",
                "refund",
                "subscription",
                "address",
                "specialist",
                "sorry",
                "thanks",
            )
        ):
            score += 0.2
        if "ord-" in text or "$" in content:
            score += 0.1
        score = min(1.0, score)
        verdict = options[0] if (options and score >= 0.6) else (options[-1] if options else "fail")
        self._sim("judge", rubric + content, verdict)
        return JudgeResult(
            score=round(score, 3), verdict=verdict, rationale="heuristic offline judge"
        )
