"""get_order ownership: inside a ticket, only the ticket customer's orders are
visible. Anything else is indistinguishable from a missing order."""

from __future__ import annotations

from sqlmodel import select

from agent_ops.backend.db import session_scope
from agent_ops.backend.models import Order
from agent_ops.tools.read_tools import GetOrderArgs, get_order
from agent_ops.tools.registry import ToolContext


def _two_customers_with_orders() -> tuple[str, str, str, str]:
    by_owner: dict[str, str] = {}
    with session_scope() as s:
        for o in s.exec(select(Order)).all():
            by_owner.setdefault(o.customer_id, o.id)
    (a, a_order), (b, b_order) = list(by_owner.items())[:2]
    return a, a_order, b, b_order


def test_ticket_customer_can_read_their_own_order():
    a, a_order, _, _ = _two_customers_with_orders()
    r = get_order(ToolContext(run_id="t", ticket_id="TCK-1", customer_id=a), GetOrderArgs(order_id=a_order))
    assert r.ok and r.data["order_id"] == a_order


def test_another_customers_order_looks_not_found():
    a, _, _, b_order = _two_customers_with_orders()
    r = get_order(ToolContext(run_id="t", ticket_id="TCK-1", customer_id=a), GetOrderArgs(order_id=b_order))
    missing = get_order(ToolContext(run_id="t", ticket_id="TCK-1", customer_id=a), GetOrderArgs(order_id="ORD-999999"))
    assert not r.ok and r.data == {}
    # Same shape of error as a genuinely missing order: existence isn't revealed.
    assert r.error == f"not_found: order {b_order}"
    assert missing.error == "not_found: order ORD-999999"


def test_ticket_without_a_known_customer_sees_no_orders():
    _, a_order, _, _ = _two_customers_with_orders()
    r = get_order(ToolContext(run_id="t", ticket_id="TCK-1", customer_id=None), GetOrderArgs(order_id=a_order))
    assert not r.ok and r.error.startswith("not_found")


def test_operator_calls_without_a_ticket_are_unrestricted():
    _, a_order, _, _ = _two_customers_with_orders()
    r = get_order(ToolContext(run_id="mcp"), GetOrderArgs(order_id=a_order))
    assert r.ok
