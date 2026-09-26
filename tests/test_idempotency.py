"""A consequential write tied to a ticket executes exactly once under
duplicate delivery, while genuinely distinct writes (e.g. two partial refunds)
still both execute. Runs against the seeded mock backend; assertions are
relative to the pre-state so they're robust to other tests mutating orders."""

from __future__ import annotations

from sqlmodel import select

from agent_ops.backend.db import session_scope
from agent_ops.backend.models import Order
from agent_ops.tools.registry import REGISTRY, ToolContext


def _pick_order(min_refundable: float) -> tuple[str, str, float]:
    with session_scope() as s:
        for o in s.exec(select(Order)).all():
            if round(o.total - o.refunded_amount, 2) >= min_refundable:
                return o.id, o.customer_id, o.refunded_amount
    raise AssertionError("seed has no order with enough refundable balance")


def _refunded(order_id: str) -> float:
    with session_scope() as s:
        order = s.get(Order, order_id)
        assert order is not None
        return order.refunded_amount


def test_duplicate_refund_executes_once():
    order_id, customer_id, before = _pick_order(5.0)
    ctx = ToolContext(run_id="rt-idem-dup", ticket_id="TCK-IDEM-DUP", customer_id=customer_id)
    args = {"order_id": order_id, "amount": 2.0, "reason": "duplicate delivery"}

    first = REGISTRY.run("issue_refund", args, ctx)
    replay = REGISTRY.run("issue_refund", args, ctx)  # same ticket + args = redelivery

    assert first.ok and replay.ok
    assert first.data.get("idempotent_replay") is None  # first time: real execution
    assert replay.data.get("idempotent_replay") is True  # second time: replayed
    assert _refunded(order_id) == round(before + 2.0, 2)  # money moved exactly once


def test_distinct_partial_refunds_not_collapsed():
    order_id, customer_id, before = _pick_order(10.0)
    ctx = ToolContext(
        run_id="rt-idem-partial", ticket_id="TCK-IDEM-PARTIAL", customer_id=customer_id
    )

    r1 = REGISTRY.run("issue_refund", {"order_id": order_id, "amount": 3.0, "reason": "a"}, ctx)
    r2 = REGISTRY.run("issue_refund", {"order_id": order_id, "amount": 4.0, "reason": "b"}, ctx)

    assert r1.ok and r2.ok
    assert not r2.data.get("idempotent_replay")  # different amount = different key = executes
    assert _refunded(order_id) == round(before + 7.0, 2)


def test_direct_call_without_ticket_is_not_deduplicated():
    # Idempotency is scoped to inbound tickets; ad-hoc/MCP calls with no ticket
    # must not be silently collapsed.
    order_id, customer_id, before = _pick_order(5.0)
    ctx = ToolContext(run_id="rt-idem-noticket", ticket_id=None, customer_id=customer_id)
    args = {"order_id": order_id, "amount": 1.0, "reason": "no ticket"}

    REGISTRY.run("issue_refund", args, ctx)
    REGISTRY.run("issue_refund", args, ctx)

    assert _refunded(order_id) == round(before + 2.0, 2)  # both applied
