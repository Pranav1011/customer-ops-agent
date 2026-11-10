"""Security-audit follow-ups: a retry never repeats writes that already
happened, money moves are atomic under concurrency, and order-scoped writes
never reveal or touch another customer's order."""

from __future__ import annotations

import threading

import pytest
from sqlmodel import select

from agent_ops.api import worker
from agent_ops.backend.db import session_scope
from agent_ops.backend.models import ActionLog, Customer, Order, Payment, Ticket
from agent_ops.policy.engine import evaluate_action
from agent_ops.tools import write_tools
from agent_ops.tools.registry import REGISTRY, ToolContext
from agent_ops.tools.write_tools import undo_action

_BODY = "Refund ORD-AUDIT please"


def _order(oid: str, cid: str, *, paid: float) -> None:
    with session_scope() as s:
        if s.get(Customer, cid) is None:
            s.add(Customer(id=cid, name="Audit", email=f"{cid.lower()}@example.com"))
        s.add(Order(id=oid, customer_id=cid, status="delivered", total=paid))
        s.add(Payment(id=f"PAY-{oid}", customer_id=cid, order_id=oid, amount=paid))


def _refunded(oid: str) -> float:
    with session_scope() as s:
        return s.get(Order, oid).refunded_amount


def _sync_redis(monkeypatch):
    fakeredis = pytest.importorskip("fakeredis")
    rq = pytest.importorskip("rq")
    from agent_ops.api import queue_redis
    from agent_ops.config import get_settings

    conn = fakeredis.FakeStrictRedis()
    q = rq.Queue(queue_redis.QUEUE_NAME, connection=conn, is_async=True)
    monkeypatch.setattr(queue_redis, "get_redis", lambda: conn)
    monkeypatch.setattr(queue_redis, "get_queue", lambda: q)
    monkeypatch.setattr(get_settings(), "queue_backend", "redis")
    return conn, q


def test_a_failure_after_a_write_is_not_retried(monkeypatch):
    conn, q = _sync_redis(monkeypatch)
    from rq import SimpleWorker

    _order("ORD-AUDIT-RETRY", "CUST-AUDIT-1", paid=50.0)

    def refund_then_crash(body, *, ticket_id, customer_id, order_id):
        ctx = ToolContext(run_id="audit-retry", ticket_id=ticket_id, customer_id=customer_id)
        args = {"order_id": "ORD-AUDIT-RETRY", "amount": 20.0, "reason": "x"}
        assert REGISTRY.run("issue_refund", args, ctx).ok
        raise RuntimeError("model timed out composing the reply")

    monkeypatch.setattr(worker, "run_ticket", refund_then_crash)
    with session_scope() as s:
        s.add(Ticket(id="TCK-AUDIT-RETRY", customer_id="CUST-AUDIT-1", body=_BODY))
    job_id = worker.enqueue(body=_BODY, ticket_id="TCK-AUDIT-RETRY", customer_id="CUST-AUDIT-1")

    SimpleWorker([q], connection=conn).work(burst=True)

    j = worker.get_job(job_id)
    assert j["status"] == "failed"
    assert j["error"].startswith("not retried: writes already applied: issue_refund")
    assert q.fetch_job(job_id).get_status() != "scheduled"  # no retry queued
    assert _refunded("ORD-AUDIT-RETRY") == 20.0
    with session_scope() as s:
        assert s.get(Ticket, "TCK-AUDIT-RETRY").resolution_status == "failed"


def test_a_failure_before_any_write_is_still_retried(monkeypatch):
    conn, q = _sync_redis(monkeypatch)
    from rq import SimpleWorker

    def crash(*a, **k):
        raise RuntimeError("model backend down")

    monkeypatch.setattr(worker, "run_ticket", crash)
    job_id = worker.enqueue(body=_BODY, ticket_id="TCK-AUDIT-NOWRITE")

    SimpleWorker([q], connection=conn).work(burst=True)

    assert worker.get_job(job_id)["status"] == "queued"
    assert q.fetch_job(job_id).get_status() == "scheduled"


def _race(fn, n: int = 2) -> list:
    barrier = threading.Barrier(n)
    out: list = [None] * n

    def run(i: int) -> None:
        barrier.wait()
        try:
            out[i] = fn()
        except Exception as e:  # surface, don't hide, a crash under contention
            out[i] = e

    threads = [threading.Thread(target=run, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return out


def test_concurrent_refunds_cannot_exceed_what_was_paid(monkeypatch):
    _order("ORD-AUDIT-RACE", "CUST-AUDIT-2", paid=50.0)
    # Hold both callers after they read the order, before either writes: the
    # interleaving that let two refunds pass a read-then-write cap check.
    gate = threading.Barrier(2, timeout=2)
    real = write_tools.refundable_remaining

    def slow_remaining(s, o):
        try:
            gate.wait()
        except threading.BrokenBarrierError:
            pass
        return real(s, o)

    monkeypatch.setattr(write_tools, "refundable_remaining", slow_remaining)
    ctx = ToolContext(run_id="audit-race", customer_id="CUST-AUDIT-2")
    args = {"order_id": "ORD-AUDIT-RACE", "amount": 30.0, "reason": "x"}

    results = _race(lambda: REGISTRY.run("issue_refund", args, ctx))

    assert not any(isinstance(r, Exception) for r in results), results
    assert sorted(r.ok for r in results) == [False, True]
    loser = next(r for r in results if not r.ok)
    assert loser.error.startswith("conflict: refund 30.0 exceeds refundable 20.0")
    assert _refunded("ORD-AUDIT-RACE") == 30.0
    with session_scope() as s:
        refunds = s.exec(
            select(Payment).where(
                Payment.order_id == "ORD-AUDIT-RACE", Payment.status == "refunded"
            )
        ).all()
        assert len(refunds) == 1


def test_concurrent_undo_reverses_once():
    _order("ORD-AUDIT-UNDO", "CUST-AUDIT-3", paid=50.0)
    ctx = ToolContext(run_id="audit-undo", customer_id="CUST-AUDIT-3")
    REGISTRY.run("issue_refund", {"order_id": "ORD-AUDIT-UNDO", "amount": 10.0, "reason": "a"}, ctx)
    REGISTRY.run("issue_refund", {"order_id": "ORD-AUDIT-UNDO", "amount": 15.0, "reason": "b"}, ctx)
    with session_scope() as s:
        rows = s.exec(select(ActionLog).where(ActionLog.run_id == "audit-undo")).all()
        first = min(r.id for r in rows)

    results = _race(lambda: undo_action(first))

    assert not any(isinstance(r, Exception) for r in results), results
    assert sorted(r.ok for r in results) == [False, True]
    assert _refunded("ORD-AUDIT-UNDO") == 15.0


def test_refund_on_another_customers_order_looks_like_a_missing_order():
    _order("ORD-AUDIT-FOREIGN", "CUST-AUDIT-OWNER", paid=50.0)
    ctx = ToolContext(run_id="audit-foreign", customer_id="CUST-AUDIT-INTRUDER")

    res = REGISTRY.run(
        "issue_refund", {"order_id": "ORD-AUDIT-FOREIGN", "amount": 5.0, "reason": "x"}, ctx
    )

    assert res.error == "not_found: order ORD-AUDIT-FOREIGN"
    assert _refunded("ORD-AUDIT-FOREIGN") == 0.0


def test_address_change_on_another_customers_order_looks_like_a_missing_order():
    with session_scope() as s:
        if s.get(Customer, "CUST-AUDIT-OWNER") is None:
            s.add(Customer(id="CUST-AUDIT-OWNER", name="Audit", email="o@example.com"))
        s.add(Order(id="ORD-AUDIT-ADDR", customer_id="CUST-AUDIT-OWNER", status="placed"))
    ctx = ToolContext(run_id="audit-addr", customer_id="CUST-AUDIT-INTRUDER")
    address = {"line1": "1 X", "city": "Y", "region": "Z", "postal_code": "1"}

    res = REGISTRY.run(
        "update_shipping_address", {"order_id": "ORD-AUDIT-ADDR", "address": address}, ctx
    )

    assert res.error == "not_found: order ORD-AUDIT-ADDR"


def test_cross_customer_block_does_not_name_the_owner():
    _order("ORD-AUDIT-SCOPE", "CUST-AUDIT-SECRET", paid=50.0)

    d = evaluate_action(
        "issue_refund",
        {"order_id": "ORD-AUDIT-SCOPE", "amount": 5.0},
        authorized_customer="CUST-AUDIT-ME",
    )

    assert d.rule == "cross_customer"
    assert "CUST-AUDIT-SECRET" not in d.reason
