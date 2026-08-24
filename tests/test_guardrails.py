"""Integration tests: guardrails firing through the full agent loop, plus
action logging and reversibility."""

from __future__ import annotations

from sqlmodel import select

import agent_ops.tools  # noqa: F401
from agent_ops.agent.graph import run_ticket
from agent_ops.backend.db import session_scope
from agent_ops.backend.models import ActionLog, Escalation, Order, Payment
from agent_ops.tools.registry import ToolContext
from agent_ops.tools.write_tools import IssueRefundArgs, issue_refund, undo_action


def _fresh_order(max_total: float | None = None, min_total: float | None = None) -> dict:
    with session_scope() as s:
        stmt = select(Order).where(Order.status == "delivered", Order.refunded_amount == 0.0)
        for o in s.exec(stmt).all():
            if max_total is not None and o.total > max_total:
                continue
            if min_total is not None and o.total < min_total:
                continue
            return {"id": o.id, "customer_id": o.customer_id, "total": o.total}
    raise AssertionError("no matching fresh order")


def test_small_refund_auto_resolved_and_logged():
    o = _fresh_order(max_total=100.0)
    r = run_ticket(
        f"Please refund order {o['id']}, it wasn't what I expected.",
        ticket_id="TCK-RF-SMALL",
        customer_id=o["customer_id"],
    )
    assert r["status"] == "resolved" and not r["escalated"]
    with session_scope() as s:
        logs = s.exec(
            select(ActionLog).where(
                ActionLog.run_id == r["run_id"], ActionLog.tool == "issue_refund"
            )
        ).all()
        assert any(x.ok for x in logs)
        order = s.get(Order, o["id"])
        assert order.refunded_amount > 0


def test_large_refund_escalates_and_takes_no_action():
    o = _fresh_order(min_total=120.0)
    r = run_ticket(
        f"Refund order {o['id']} in full please.",
        ticket_id="TCK-RF-LARGE",
        customer_id=o["customer_id"],
    )
    assert r["status"] == "escalated" and r["escalated"]
    with session_scope() as s:
        # No successful refund should have been executed for this run.
        logs = s.exec(
            select(ActionLog).where(
                ActionLog.run_id == r["run_id"],
                ActionLog.tool == "issue_refund",
            )
        ).all()
        assert not any(x.ok for x in logs)
        order = s.get(Order, o["id"])
        assert order.refunded_amount == 0


def test_cancellation_escalates():
    r = run_ticket(
        "Please cancel my subscription now.", ticket_id="TCK-CX", customer_id="CUST-00005"
    )
    assert r["escalated"]
    with session_scope() as s:
        assert (
            s.exec(select(Escalation).where(Escalation.ticket_id == "TCK-CX")).first() is not None
        )


def test_refund_is_reversible():
    o = _fresh_order(max_total=100.0)
    ctx = ToolContext(run_id="rev-test", ticket_id="TCK-REV", customer_id=o["customer_id"])
    res = issue_refund(ctx, IssueRefundArgs(order_id=o["id"], amount=5.0, reason="test"))
    assert res.ok
    with session_scope() as s:
        log = s.exec(
            select(ActionLog).where(
                ActionLog.run_id == "rev-test", ActionLog.tool == "issue_refund"
            )
        ).first()
        action_id = log.id
        refund_payment_id = res.data["refund_id"]
        assert s.get(Order, o["id"]).refunded_amount == 5.0

    undo = undo_action(action_id)
    assert undo.ok
    with session_scope() as s:
        assert s.get(Order, o["id"]).refunded_amount == 0.0
        assert s.get(Payment, refund_payment_id) is None
