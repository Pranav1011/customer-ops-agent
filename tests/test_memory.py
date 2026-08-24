"""Tests for long-term memory (episodic + semantic) and short-term compaction."""

from __future__ import annotations

from sqlmodel import select

from agent_ops.agent.graph import run_ticket
from agent_ops.backend.db import session_scope
from agent_ops.backend.models import Order
from agent_ops.memory.long_term import load_profile, recall, record_resolution
from agent_ops.memory.short_term import compact_scratchpad


def _a_shipped_order() -> dict:
    with session_scope() as s:
        o = s.exec(select(Order).where(Order.status == "shipped")).first()
        return {"id": o.id, "customer_id": o.customer_id}


def test_record_and_recall_builds_profile():
    cid = "CUST-00007"
    record_resolution(cid, {"intent": "refund", "status": "resolved", "summary": "refunded"})
    record_resolution(cid, {"intent": "order_status", "status": "resolved", "summary": "wismo"})
    mem = recall(cid)
    assert mem["interaction_count"] == 2
    assert mem["returning_customer"] is True
    assert mem["semantic"]["refund_requests"] == 1
    assert "tier" in mem["semantic"]  # derived from the Customer record
    assert len(mem["recent_resolutions"]) == 2


def test_episodic_is_capped():
    cid = "CUST-00008"
    for i in range(25):
        record_resolution(cid, {"intent": "order_status", "status": "resolved", "summary": f"n{i}"})
    prof = load_profile(cid)
    assert len(prof["episodic"]) == 20  # capped
    assert prof["semantic"]["interactions"] == 20


def test_agent_run_writes_memory_and_second_run_recalls_it():
    o = _a_shipped_order()
    cid = o["customer_id"]
    before = recall(cid)["interaction_count"]
    run_ticket(f"where is my order {o['id']}", ticket_id="TCK-MEM1", customer_id=cid)
    after = recall(cid)["interaction_count"]
    assert after == before + 1

    # The next run should recall the prior interaction in its trace.
    r2 = run_ticket(f"tracking for {o['id']}", ticket_id="TCK-MEM2", customer_id=cid)
    import json
    from pathlib import Path

    trace = json.loads(Path(r2["trace_path"]).read_text())
    recall_events = [e for e in trace["events"] if e["type"] == "memory_recall"]
    assert recall_events and recall_events[0]["memory"]["interaction_count"] >= 1


def test_compaction_shrinks_long_scratchpad_but_keeps_signal():
    big = {"orders": [{"order_id": f"ORD-{i:06d}", "junk": "x" * 100} for i in range(30)]}
    scratchpad = []
    for _ in range(10):
        scratchpad.append(
            {
                "tool": "find_orders",
                "args": {"customer_id": "C"},
                "result": {"ok": True, "data": big},
            }
        )
    # Add a recent, must-keep entry.
    scratchpad.append(
        {
            "tool": "get_order",
            "args": {"order_id": "ORD-1"},
            "result": {"ok": True, "data": {"status": "shipped"}},
        }
    )

    compacted, n = compact_scratchpad(scratchpad, keep_last=6)
    assert n > 0
    # Compacted entries preserve tool + ok, drop the heavy payload.
    for entry in compacted:
        if entry["result"]["data"].get("_compacted"):
            assert entry["tool"] == "find_orders"
            assert entry["result"]["ok"] is True
    # The recent get_order entry is untouched.
    assert compacted[-1]["result"]["data"]["status"] == "shipped"


def test_compaction_noop_for_short_scratchpad():
    scratchpad = [{"tool": "get_order", "args": {}, "result": {"ok": True, "data": {"x": 1}}}]
    compacted, n = compact_scratchpad(scratchpad)
    assert n == 0 and compacted == scratchpad
