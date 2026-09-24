"""The queue-backend switch.

The default thread backend still resolves a ticket off the request path; the
Redis/RQ backend enqueues and runs the same `process_job`. The Redis path is
exercised against fakeredis with a synchronous queue, so the test needs no Redis
server and no separate worker process.
"""

from __future__ import annotations

import time

import pytest

from agent_ops.api import worker


def _wait_terminal(job_id: str, tries: int = 60) -> dict:
    for _ in range(tries):
        j = worker.get_job(job_id)
        if j and j["status"] in ("succeeded", "failed"):
            return j
        time.sleep(0.1)
    return worker.get_job(job_id) or {}


def test_thread_backend_processes_job():
    job_id = worker.enqueue(
        body="Where is my order ORD-000001?", ticket_id="TCK-Q-THREAD", customer_id=None
    )
    j = _wait_terminal(job_id)
    assert j.get("status") == "succeeded"


def test_redis_backend_enqueues_and_processes(monkeypatch):
    fakeredis = pytest.importorskip("fakeredis")
    rq = pytest.importorskip("rq")

    from agent_ops.api import queue_redis
    from agent_ops.config import get_settings

    conn = fakeredis.FakeStrictRedis()
    sync_queue = rq.Queue(queue_redis.QUEUE_NAME, connection=conn, is_async=False)  # runs inline
    monkeypatch.setattr(queue_redis, "get_queue", lambda: sync_queue)
    monkeypatch.setattr(get_settings(), "queue_backend", "redis")

    job_id = worker.enqueue(
        body="Where is my order ORD-000001?", ticket_id="TCK-Q-REDIS", customer_id=None
    )
    j = worker.get_job(job_id)
    assert j is not None
    assert j["status"] == "succeeded"  # processed via the RQ path, not the thread pool
