"""Integration tests for the agent loop against the seeded mock backend."""

from __future__ import annotations

from pathlib import Path

from sqlmodel import select

from agent_ops.agent.graph import run_ticket
from agent_ops.backend.db import session_scope
from agent_ops.backend.models import Escalation, Order


def _a_shipped_order() -> dict:
    with session_scope() as s:
        o = s.exec(select(Order).where(Order.status == "shipped")).first()
        return {"id": o.id, "customer_id": o.customer_id, "status": o.status}


def test_wismo_resolves_grounded_in_order():
    o = _a_shipped_order()
    oid = o["id"]
    r = run_ticket(
        f"Where is my order {oid}? It hasn't arrived.",
        ticket_id="TCK-WISMO",
        customer_id=o["customer_id"],
    )
    assert r["intent"] == "order_status"
    assert r["status"] == "resolved"
    assert not r["escalated"]
    # Reply must be grounded in the real order id (no hallucination).
    assert oid in r["customer_reply"]
    assert Path(r["trace_path"]).exists()
    # Efficiency metrics are populated.
    assert r["summary"]["tool_calls"] >= 1
    assert r["summary"]["total_cost_usd"] >= 0.0


def test_unimplemented_intent_escalates_safely():
    r = run_ticket(
        "Please cancel my subscription.",
        ticket_id="TCK-CANCEL",
        customer_id="CUST-00002",
    )
    assert r["intent"] == "cancel_subscription"
    assert r["status"] == "escalated"
    assert r["escalated"]
    with session_scope() as s:
        rows = s.exec(select(Escalation).where(Escalation.ticket_id == "TCK-CANCEL")).all()
    assert len(rows) == 1


def test_missing_order_id_falls_back_to_recent_orders():
    o = _a_shipped_order()
    r = run_ticket(
        "Where is my order? It still hasn't arrived and I'm getting worried.",
        ticket_id="TCK-NOID",
        customer_id=o["customer_id"],
    )
    assert r["intent"] == "order_status"
    # With no order id, the agent falls back to the customer's recent orders.
    assert r["status"] in ("resolved", "escalated")
    # It must have called find_orders during the fallback.
    import json

    trace = json.loads(Path(r["trace_path"]).read_text())
    tools = [e.get("tool") for e in trace["events"] if e["type"] == "tool_call"]
    assert "find_orders" in tools


def test_trace_is_reconstructable():
    o = _a_shipped_order()
    r = run_ticket(f"track order {o['id']}", ticket_id="TCK-TRACE", customer_id=o["customer_id"])
    import json

    trace = json.loads(Path(r["trace_path"]).read_text())
    types = [e["type"] for e in trace["events"]]
    assert "intent" in types and "plan" in types and "reply" in types
    assert trace["plan"]["intent"] == "order_status"
