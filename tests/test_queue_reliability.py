"""Crash recovery + dead-letter.

Simulates a worker that died mid-job — a Job row stuck 'running' with its payload
persisted and nothing live in a fresh queue — and asserts reconcile_orphans()
re-queues it from that payload and runs it to completion (the "survives kill -9"
guarantee). Also asserts a permanently-failing job lands in the dead-letter
registry instead of vanishing. Exercised against fakeredis with a synchronous
queue: no Redis server, no separate worker.
"""

from __future__ import annotations

import pytest

from agent_ops.backend.db import session_scope
from agent_ops.backend.models import Job


def _always_fails() -> None:
    raise RuntimeError("poison message")


def _use_sync_redis(monkeypatch):
    fakeredis = pytest.importorskip("fakeredis")
    rq = pytest.importorskip("rq")
    from agent_ops.api import queue_redis
    from agent_ops.config import get_settings

    conn = fakeredis.FakeStrictRedis()
    sync_q = rq.Queue(queue_redis.QUEUE_NAME, connection=conn, is_async=False)
    monkeypatch.setattr(queue_redis, "get_redis", lambda: conn)
    monkeypatch.setattr(queue_redis, "get_queue", lambda: sync_q)
    monkeypatch.setattr(get_settings(), "queue_backend", "redis")
    return conn, sync_q


def test_orphaned_job_is_requeued_and_completes_on_restart(monkeypatch):
    _use_sync_redis(monkeypatch)
    from agent_ops.api import worker
    from agent_ops.reliability.reconcile import reconcile_orphans

    job_id = "JOB-ORPHAN1"
    with session_scope() as s:  # a job the dead worker left mid-flight
        s.add(
            Job(
                id=job_id,
                ticket_id="TCK-ORPH",
                status="running",
                payload={
                    "body": "Where is my order ORD-000001?",
                    "customer_id": None,
                    "order_id": None,
                },
            )
        )

    requeued = reconcile_orphans()

    assert job_id in requeued
    assert worker.get_job(job_id)["status"] == "succeeded"  # resumed, not stuck 'running'


def test_permanently_failing_job_lands_in_dead_letter(monkeypatch):
    _, sync_q = _use_sync_redis(monkeypatch)
    from agent_ops.reliability.reconcile import dead_letter_ids

    job = sync_q.enqueue(_always_fails)  # inline execution -> raises -> FailedJobRegistry

    assert job.id in dead_letter_ids()
