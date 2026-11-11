"""Action claims: a customer reply may only say a refund, cancellation, address change
or credit is done if that write succeeded in this run. Covers the policy function and
the resolve-node guard, on resolved and on already-escalated runs."""

from __future__ import annotations

import pytest
from sqlmodel import select

from agent_ops.agent.schemas import Decision, DecisionAction
from agent_ops.backend.db import session_scope
from agent_ops.backend.models import Order, Payment
from agent_ops.policy.action_claims import unbacked_action_claims
from tests.test_guardrails import _run_with, _StubProvider

# Replies llama3.1:8b drafted on escalated double-charge runs that wrote nothing
# (abridged; the second is the browser-test report, the first from this fix's E2E run).
_LOGGED_PROGRESSIVE = (
    "We've located the duplicate charge on your order ORD-000664. We're processing a refund "
    "for this amount as per our policy for unverified identity."
)
_LOGGED_REPLY = (
    "We've processed your request for a refund of the duplicate charge on order ORD-000664. "
    "You should see the funds back in your account within 5-7 business days."
)


# --------------------------------------------------------------------------- #
# policy function
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("reply", "tool"),
    [
        (_LOGGED_REPLY, "issue_refund"),
        (_LOGGED_PROGRESSIVE, "issue_refund"),
        ("Your refund is being processed now.", "issue_refund"),
        ("I'm cancelling your subscription today.", "cancel_subscription"),
        ("We've refunded the duplicate charge.", "issue_refund"),
        ("I have issued a full refund to your card.", "issue_refund"),
        ("Your refund of $24.99 has been processed.", "issue_refund"),
        ("The refund has been issued to your original payment method.", "issue_refund"),
        ("Your order has been refunded.", "issue_refund"),
        ("I’ve cancelled your subscription.", "cancel_subscription"),
        ("Your subscription has been cancelled.", "cancel_subscription"),
        ("I've updated your shipping address.", "update_shipping_address"),
        ("The delivery address has been updated to 1 Main St.", "update_shipping_address"),
        ("We've applied a $10 account credit.", "apply_account_credit"),
        ("A credit has been applied to your account.", "apply_account_credit"),
    ],
)
def test_completed_action_without_a_write_is_unbacked(reply, tool):
    assert unbacked_action_claims(reply, []) == [tool]


def test_claim_backed_by_a_successful_write_passes():
    assert unbacked_action_claims("We've refunded the duplicate charge.", ["issue_refund"]) == []


def test_a_different_write_does_not_back_the_claim():
    reply = "I've cancelled your subscription."
    assert unbacked_action_claims(reply, ["issue_refund"]) == ["cancel_subscription"]


@pytest.mark.parametrize(
    "reply",
    [
        "We will look into a refund for the duplicate charge.",
        "A specialist will review your refund request and follow up.",
        "Once your refund has been processed, it posts within 5-10 business days.",
        "If your subscription has been cancelled, you won't be billed again.",
        "We have not refunded this order yet.",
        "We're reviewing your refund request.",
        "You can expect the refund within 5-7 business days once it is approved.",
        "I can request a refund, and a specialist will confirm it.",
        "Thanks for reaching out. I've routed your request to a specialist on our team.",
        "I took a close look at the payments and didn't find a duplicate charge.",
    ],
)
def test_future_conditional_and_neutral_wording_is_not_a_claim(reply):
    assert unbacked_action_claims(reply, []) == []


# --------------------------------------------------------------------------- #
# resolve-node guard
# --------------------------------------------------------------------------- #
def _guard_fired(final) -> bool:
    return any(e.get("decision") == "action_claim_fail" for e in final["trace_events"])


def test_escalated_run_cannot_claim_a_refund():
    # The logged failure: the loop breaker escalated before any write, and the
    # model's drafted reply still told the customer the refund was processed.
    class EscalateButClaim(_StubProvider):
        def decide(self, view):
            return Decision(action=DecisionAction.escalate, args={"reason": "unsure"})

        def compose_reply(self, view):
            return "We've processed your request for a refund of the duplicate charge."

    final = _run_with(EscalateButClaim(), run_id="claim-escalated", customer_id="CUST-00001")
    reply = final["resolution"]["customer_reply"]
    assert final["escalated"] is True
    assert _guard_fired(final)
    assert "refund" not in reply.lower()
    assert "specialist who will follow up" in reply


def test_resolved_run_claiming_an_unwritten_refund_escalates():
    class ClaimOnly(_StubProvider):
        def compose_reply(self, view):
            return "You're all set, I've issued a refund to your card."

    final = _run_with(ClaimOnly(), run_id="claim-resolved", customer_id="CUST-00001")
    assert final["escalated"] is True
    assert final["stop_reason"] == "escalate"
    assert final["resolution"]["status"] == "escalated"
    assert _guard_fired(final)
    assert "refund" not in final["resolution"]["customer_reply"].lower()


def test_future_wording_on_a_resolved_run_is_left_alone():
    class Future(_StubProvider):
        def compose_reply(self, view):
            return "A specialist will review your refund request and follow up by email."

    final = _run_with(Future(), run_id="claim-future", customer_id="CUST-00001")
    assert final["escalated"] is False
    assert not _guard_fired(final)
    assert final["resolution"]["customer_reply"].startswith("A specialist will review")


def test_claim_backed_by_a_refund_in_this_run_is_left_alone():
    with session_scope() as s:
        paid = {
            p.order_id for p in s.exec(select(Payment).where(Payment.status == "succeeded")).all()
        }
        order = next(
            o for o in s.exec(select(Order)).all() if o.id in paid and o.refunded_amount == 0
        )
        oid, cid = order.id, order.customer_id

    class Refunds(_StubProvider):
        done = False

        def decide(self, view):
            if not self.done:
                self.done = True
                return Decision(
                    action=DecisionAction.call_tool,
                    tool="issue_refund",
                    args={"order_id": oid, "amount": 1.0, "reason": "duplicate charge"},
                    confidence=1.0,
                )
            return Decision(action=DecisionAction.finish)

        def compose_reply(self, view):
            return f"We've refunded $1.00 on order {oid}."

    final = _run_with(Refunds(), run_id="claim-backed", customer_id=cid)
    assert final["actions_taken"] == ["issue_refund"]
    assert not _guard_fired(final)
    assert final["escalated"] is False
    assert final["resolution"]["customer_reply"] == f"We've refunded $1.00 on order {oid}."
