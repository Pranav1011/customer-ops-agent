"""Money and subscription status rules.

A refund is capped by what the customer actually paid on the order, a
cancellation is a one-way move, and undoing a refund really undoes it: the
same refund can be issued again and it moves money again.
"""

from __future__ import annotations

from sqlmodel import select

from agent_ops.backend.db import session_scope
from agent_ops.backend.models import ActionLog, Customer, Order, Payment, Subscription
from agent_ops.policy.engine import evaluate_action
from agent_ops.tools.registry import REGISTRY, ToolContext
from agent_ops.tools.write_tools import undo_action


def _customer(cid: str) -> None:
    with session_scope() as s:
        if s.get(Customer, cid) is None:
            s.add(Customer(id=cid, name="Rules Test", email=f"{cid.lower()}@example.com"))


def _order(oid: str, cid: str, *, total: float, status: str, paid: list[float]) -> None:
    _customer(cid)
    with session_scope() as s:
        s.add(Order(id=oid, customer_id=cid, status=status, total=total))
        for i, amount in enumerate(paid, start=1):
            s.add(
                Payment(
                    id=f"PAY-{oid}-{i}",
                    customer_id=cid,
                    order_id=oid,
                    amount=amount,
                    status="succeeded",
                )
            )


def _refunded(oid: str) -> float:
    with session_scope() as s:
        return s.get(Order, oid).refunded_amount


def _action_id(run_id: str, tool: str) -> int:
    with session_scope() as s:
        return (
            s.exec(select(ActionLog).where(ActionLog.run_id == run_id, ActionLog.tool == tool))
            .first()
            .id
        )


def test_refund_on_an_order_that_was_never_paid_is_refused():
    _order("ORD-UNPAID-1", "CUST-RULES-1", total=40.0, status="cancelled", paid=[])
    ctx = ToolContext(run_id="rules-unpaid", customer_id="CUST-RULES-1")

    res = REGISTRY.run(
        "issue_refund", {"order_id": "ORD-UNPAID-1", "amount": 40.0, "reason": "cancelled"}, ctx
    )

    assert not res.ok
    assert res.error.startswith("conflict:")
    assert "paid 0.0" in res.error
    assert _refunded("ORD-UNPAID-1") == 0.0


def test_policy_blocks_a_refund_beyond_what_was_paid():
    _order("ORD-UNPAID-2", "CUST-RULES-2", total=40.0, status="cancelled", paid=[])

    decision = evaluate_action(
        "issue_refund",
        {"order_id": "ORD-UNPAID-2", "amount": 10.0, "reason": "x"},
        authorized_customer="CUST-RULES-2",
    )

    assert decision.effect == "block"
    assert decision.rule == "refund_exceeds_order"


def test_refund_on_a_paid_cancelled_order_is_still_allowed():
    _order("ORD-PAIDCX-1", "CUST-RULES-3", total=40.0, status="cancelled", paid=[40.0])
    ctx = ToolContext(run_id="rules-paidcx", customer_id="CUST-RULES-3")

    res = REGISTRY.run(
        "issue_refund", {"order_id": "ORD-PAIDCX-1", "amount": 40.0, "reason": "cancelled"}, ctx
    )

    assert res.ok
    assert _refunded("ORD-PAIDCX-1") == 40.0


def test_a_double_charge_can_be_refunded_on_top_of_the_order_total():
    _order("ORD-DOUBLE-1", "CUST-RULES-4", total=50.0, status="delivered", paid=[50.0, 50.0])
    ctx = ToolContext(run_id="rules-double", customer_id="CUST-RULES-4")

    first = REGISTRY.run(
        "issue_refund", {"order_id": "ORD-DOUBLE-1", "amount": 50.0, "reason": "dup"}, ctx
    )
    second = REGISTRY.run(
        "issue_refund", {"order_id": "ORD-DOUBLE-1", "amount": 50.0, "reason": "item"}, ctx
    )
    third = REGISTRY.run(
        "issue_refund", {"order_id": "ORD-DOUBLE-1", "amount": 0.5, "reason": "more"}, ctx
    )

    assert first.ok and second.ok
    assert not third.ok
    assert _refunded("ORD-DOUBLE-1") == 100.0


def test_undone_refund_can_be_issued_again_on_the_same_ticket():
    _order("ORD-UNDO-1", "CUST-RULES-5", total=30.0, status="delivered", paid=[30.0])
    ctx = ToolContext(run_id="rules-undo", ticket_id="TCK-RULES-UNDO", customer_id="CUST-RULES-5")
    args = {"order_id": "ORD-UNDO-1", "amount": 10.0, "reason": "late"}

    first = REGISTRY.run("issue_refund", args, ctx)
    assert first.ok and _refunded("ORD-UNDO-1") == 10.0
    assert undo_action(_action_id("rules-undo", "issue_refund")).ok
    assert _refunded("ORD-UNDO-1") == 0.0

    again = REGISTRY.run("issue_refund", args, ctx)

    assert again.ok
    assert not again.data.get("idempotent_replay")  # a real refund, not a replayed "ok"
    assert again.data["refund_id"] != first.data["refund_id"]
    assert _refunded("ORD-UNDO-1") == 10.0


def test_undoing_an_earlier_refund_keeps_later_refunds():
    _order("ORD-UNDO-2", "CUST-RULES-6", total=30.0, status="delivered", paid=[30.0])
    ctx = ToolContext(run_id="rules-undo-order", customer_id="CUST-RULES-6")
    REGISTRY.run("issue_refund", {"order_id": "ORD-UNDO-2", "amount": 3.0, "reason": "a"}, ctx)
    REGISTRY.run("issue_refund", {"order_id": "ORD-UNDO-2", "amount": 4.0, "reason": "b"}, ctx)
    with session_scope() as s:
        first_id = (
            s.exec(
                select(ActionLog)
                .where(ActionLog.run_id == "rules-undo-order")
                .order_by(ActionLog.id)  # type: ignore[arg-type]
            )
            .first()
            .id
        )

    assert undo_action(first_id).ok
    assert _refunded("ORD-UNDO-2") == 4.0


def _subscription(sid: str, cid: str) -> None:
    _customer(cid)
    with session_scope() as s:
        s.add(Subscription(id=sid, customer_id=cid, status="active", monthly_amount=29.0))


def test_cancelling_twice_is_a_conflict():
    _subscription("SUB-RULES-1", "CUST-RULES-7")
    ctx = ToolContext(run_id="rules-cancel", customer_id="CUST-RULES-7")

    first = REGISTRY.run("cancel_subscription", {"customer_id": "CUST-RULES-7"}, ctx)
    second = REGISTRY.run("cancel_subscription", {"customer_id": "CUST-RULES-7"}, ctx)

    assert first.ok
    assert not second.ok
    assert second.error == (
        "conflict: subscription SUB-RULES-1 is already cancelled (legal next: none)"
    )


def test_cancellation_is_logged_as_irreversible():
    _subscription("SUB-RULES-2", "CUST-RULES-8")
    ctx = ToolContext(run_id="rules-cancel-log", customer_id="CUST-RULES-8")

    assert REGISTRY.run("cancel_subscription", {"customer_id": "CUST-RULES-8"}, ctx).ok

    action_id = _action_id("rules-cancel-log", "cancel_subscription")
    with session_scope() as s:
        assert s.get(ActionLog, action_id).reversible is False
    undo = undo_action(action_id)
    assert not undo.ok
    assert undo.error == f"conflict: action {action_id} (cancel_subscription) is irreversible"
