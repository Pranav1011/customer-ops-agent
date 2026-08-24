"""Tests for the read tools and the registry (deterministic against the seed)."""

from __future__ import annotations

from sqlmodel import select

import agent_ops.tools  # noqa: F401  (register tools)
from agent_ops.backend.db import session_scope
from agent_ops.backend.models import Order
from agent_ops.tools.registry import REGISTRY, ToolContext

CTX = ToolContext(run_id="test", ticket_id="TCK-TEST", customer_id=None)


def _a_shipped_order() -> dict:
    with session_scope() as s:
        o = s.exec(select(Order).where(Order.status == "shipped")).first()
        return {"id": o.id, "customer_id": o.customer_id, "status": o.status}


def test_read_tools_registered():
    for name in [
        "get_order",
        "find_orders",
        "get_customer",
        "get_customer_history",
        "get_subscription",
        "get_payment_history",
        "search_knowledge_base",
    ]:
        assert REGISTRY.get(name) is not None
        assert REGISTRY.get(name).kind == "read"


def test_get_order_ok_and_not_found():
    o = _a_shipped_order()
    res = REGISTRY.run("get_order", {"order_id": o["id"]}, CTX)
    assert res.ok and res.data["order_id"] == o["id"]
    assert res.data["status"] == "shipped"

    missing = REGISTRY.run("get_order", {"order_id": "ORD-999999"}, CTX)
    assert not missing.ok and "not_found" in missing.error


def test_invalid_args_are_rejected_not_raised():
    res = REGISTRY.run("get_order", {}, CTX)  # missing required order_id
    assert not res.ok and "invalid_args" in res.error


def test_find_orders_scoped_to_customer():
    o = _a_shipped_order()
    res = REGISTRY.run("find_orders", {"customer_id": o["customer_id"]}, CTX)
    assert res.ok
    assert all(x["customer_id"] == o["customer_id"] for x in res.data["orders"])


def test_kb_search_returns_relevant_policy():
    res = REGISTRY.run(
        "search_knowledge_base", {"query": "refund over 100 dollars approval", "k": 3}, CTX
    )
    assert res.ok and res.data["count"] >= 1
    titles = " ".join(h["title"].lower() for h in res.data["results"])
    assert "refund" in titles


def test_unknown_tool():
    res = REGISTRY.run("does_not_exist", {}, CTX)
    assert not res.ok and "unknown_tool" in res.error
