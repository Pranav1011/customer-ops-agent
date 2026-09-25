"""Reply scope: a customer reply may only reference the ticket customer's own orders
and account. Covers the policy function, the resolve-node guard (including tickets
that are already escalated), and the eval metric that scores reply content."""

from __future__ import annotations

from sqlmodel import select

from agent_ops.agent.schemas import Decision, DecisionAction
from agent_ops.backend.db import session_scope
from agent_ops.backend.models import Customer, Order
from agent_ops.eval import metrics
from agent_ops.policy.reply_scope import reply_scope_violations
from tests.test_guardrails import _run_with, _StubProvider


def _two_customers_with_orders() -> tuple[str, str, str, str]:
    """(customer A, A's order, customer B, B's order) from the seeded backend."""
    by_owner: dict[str, str] = {}
    with session_scope() as s:
        for o in s.exec(select(Order)).all():
            by_owner.setdefault(o.customer_id, o.id)
    (a, a_order), (b, b_order) = list(by_owner.items())[:2]
    return a, a_order, b, b_order


def _email(customer_id: str) -> str:
    with session_scope() as s:
        return s.get(Customer, customer_id).email


# --------------------------------------------------------------------------- #
# policy function
# --------------------------------------------------------------------------- #
def test_own_order_and_email_are_in_scope():
    a, a_order, _, _ = _two_customers_with_orders()
    reply = f"Your order {a_order} shipped; we'll email {_email(a)} with tracking."
    assert reply_scope_violations(reply, a) == []


def test_another_customers_order_is_out_of_scope():
    a, _, _, b_order = _two_customers_with_orders()
    v = reply_scope_violations(f"Order {b_order} was delivered yesterday.", a)
    assert v == [f"{b_order}: belongs to another customer"]


def test_unknown_order_is_out_of_scope():
    a, _, _, _ = _two_customers_with_orders()
    assert reply_scope_violations("Order ORD-999999 is on its way.", a) == ["ORD-999999: no such order"]


def test_other_customer_id_and_email_are_out_of_scope():
    a, _, b, _ = _two_customers_with_orders()
    v = reply_scope_violations(f"I checked account {b} and wrote to {_email(b)}.", a)
    assert f"{b}: another customer's account" in v
    assert f"{_email(b).lower()}: not this customer's email" in v


def test_no_references_means_no_violations():
    assert reply_scope_violations("Thanks, a specialist will follow up shortly.", None) == []


# --------------------------------------------------------------------------- #
# resolve-node guard
# --------------------------------------------------------------------------- #
def test_guard_blocks_foreign_order_in_a_resolved_reply():
    a, _, _, b_order = _two_customers_with_orders()

    class Leaky(_StubProvider):
        # Mirrors the logged failure: the agent fetches an order that isn't the
        # customer's, so the reply is "grounded" in a tool result and the old
        # grounding check lets it through.
        fetched = False

        def decide(self, view):
            if not self.fetched:
                self.fetched = True
                return Decision(
                    action=DecisionAction.call_tool, tool="get_order", args={"order_id": b_order}, confidence=1.0
                )
            return Decision(action=DecisionAction.finish)

        def compose_reply(self, view):
            return f"Good news: order {b_order} was delivered on Monday."

    final = _run_with(Leaky(), run_id="scope-resolved", customer_id=a)
    assert final["escalated"] is True
    assert any(e.get("decision") == "reply_scope_fail" for e in final["trace_events"])
    assert b_order not in final["resolution"]["customer_reply"]


def test_guard_also_runs_when_the_ticket_is_already_escalated():
    # The logged failure: the agent escalated, but the drafted reply still
    # described another customer's order. The old grounding check skipped
    # escalated tickets entirely.
    a, _, _, b_order = _two_customers_with_orders()

    class EscalateButLeak(_StubProvider):
        def decide(self, view):
            return Decision(action=DecisionAction.escalate, args={"reason": "unsure"})

        def compose_reply(self, view):
            return f"Order {b_order} was delivered on October 11th; a specialist will follow up."

    final = _run_with(EscalateButLeak(), run_id="scope-escalated", customer_id=a)
    assert final["escalated"] is True
    assert any(e.get("decision") == "reply_scope_fail" for e in final["trace_events"])
    assert b_order not in final["resolution"]["customer_reply"]


def test_in_scope_reply_is_left_alone():
    a, a_order, _, _ = _two_customers_with_orders()

    class Clean(_StubProvider):
        fetched = False

        def decide(self, view):
            if not self.fetched:
                self.fetched = True
                return Decision(
                    action=DecisionAction.call_tool, tool="get_order", args={"order_id": a_order}, confidence=1.0
                )
            return Decision(action=DecisionAction.finish)

        def compose_reply(self, view):
            return f"Your order {a_order} is on its way."

    final = _run_with(Clean(), run_id="scope-clean", customer_id=a)
    assert not any(e.get("decision") == "reply_scope_fail" for e in final["trace_events"])
    assert a_order in final["resolution"]["customer_reply"]


# --------------------------------------------------------------------------- #
# eval metric
# --------------------------------------------------------------------------- #
def test_eval_scores_reply_scope_separately_from_action_safety():
    a, _, _, b_order = _two_customers_with_orders()
    scenario = {"id": "t", "tags": [], "setup": {"customer": {"id": a}}, "expect": {"judge": False}}
    result = {"customer_reply": f"Order {b_order} was delivered.", "escalated": False, "status": "resolved"}
    rec = metrics.evaluate(scenario, result, {"events": [], "summary": {}}, None)
    assert rec["safe"] is True  # no forbidden write happened
    assert rec["reply_scope_ok"] is False
    assert rec["reply_scope_notes"] == [f"{b_order}: belongs to another customer"]
