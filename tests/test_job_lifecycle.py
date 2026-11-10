"""The Job status machine: queued -> running -> succeeded | failed.

Covers the moves the worker must refuse (re-running a finished job on duplicate
delivery), startup crash recovery wired into the API lifespan, and failures
surfacing to RQ so its retry and dead-letter machinery actually runs.
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from agent_ops.api import worker
from agent_ops.api.main import app
from agent_ops.backend.db import session_scope
from agent_ops.backend.models import Job, Ticket
from agent_ops.backend.transitions import JOB_TRANSITIONS, IllegalTransition, check_transition

_PAYLOAD = {"body": "Where is my order ORD-000001?", "customer_id": None, "order_id": None}


def _add_job(job_id: str, status: str, ticket_id: str, **fields) -> None:
    with session_scope() as s:
        s.add(Job(id=job_id, ticket_id=ticket_id, status=status, payload=_PAYLOAD, **fields))


def _add_ticket(ticket_id: str) -> None:
    with session_scope() as s:
        s.add(Ticket(id=ticket_id, body=_PAYLOAD["body"], status="open"))


def _boom(*args, **kwargs):
    raise RuntimeError("model backend down")


def _sync_redis(monkeypatch, *, is_async: bool):
    fakeredis = pytest.importorskip("fakeredis")
    rq = pytest.importorskip("rq")
    from agent_ops.api import queue_redis
    from agent_ops.config import get_settings

    conn = fakeredis.FakeStrictRedis()
    q = rq.Queue(queue_redis.QUEUE_NAME, connection=conn, is_async=is_async)
    monkeypatch.setattr(queue_redis, "get_redis", lambda: conn)
    monkeypatch.setattr(queue_redis, "get_queue", lambda: q)
    monkeypatch.setattr(get_settings(), "queue_backend", "redis")
    return conn, q


def test_illegal_job_transition_names_state_target_and_legal_moves():
    with pytest.raises(IllegalTransition) as e:
        check_transition(JOB_TRANSITIONS, "job", "JOB-X", "succeeded", "running")
    assert str(e.value) == (
        "conflict: job JOB-X is succeeded; cannot move to running (legal next: none)"
    )


def test_duplicate_delivery_of_a_finished_job_does_not_rerun_it(monkeypatch):
    calls = []
    monkeypatch.setattr(worker, "run_ticket", lambda *a, **k: calls.append(a))
    _add_job("JOB-DUP-DONE", "succeeded", "TCK-DUP-DONE", run_id="run-original")

    worker.process_job("JOB-DUP-DONE", _PAYLOAD["body"], "TCK-DUP-DONE", None, None)

    assert calls == []
    j = worker.get_job("JOB-DUP-DONE")
    assert j["status"] == "succeeded"
    assert j["run_id"] == "run-original"


def test_api_startup_requeues_orphaned_jobs():
    # Thread backend: the in-process pool died with the previous process, so a
    # row left 'running' at startup is an orphan that only reconcile can rescue.
    _add_job("JOB-LIFESPAN-ORPHAN", "running", "TCK-LIFESPAN-ORPHAN")

    with TestClient(app):
        for _ in range(80):
            if worker.get_job("JOB-LIFESPAN-ORPHAN")["status"] in ("succeeded", "failed"):
                break
            time.sleep(0.1)

    assert worker.get_job("JOB-LIFESPAN-ORPHAN")["status"] == "succeeded"


def test_failed_job_marks_ticket_failed_on_thread_backend(monkeypatch):
    monkeypatch.setattr(worker, "run_ticket", _boom)
    _add_ticket("TCK-FAIL-THREAD")
    _add_job("JOB-FAIL-THREAD", "queued", "TCK-FAIL-THREAD")

    worker.process_job("JOB-FAIL-THREAD", _PAYLOAD["body"], "TCK-FAIL-THREAD", None, None)

    j = worker.get_job("JOB-FAIL-THREAD")
    assert j["status"] == "failed"
    assert "model backend down" in j["error"]
    with session_scope() as s:
        assert s.get(Ticket, "TCK-FAIL-THREAD").resolution_status == "failed"


def test_redis_failure_reaches_the_dead_letter_queue(monkeypatch):
    _sync_redis(monkeypatch, is_async=False)
    from agent_ops.reliability.reconcile import dead_letter_ids

    monkeypatch.setattr(worker, "run_ticket", _boom)
    _add_ticket("TCK-FAIL-REDIS")

    job_id = worker.enqueue(body=_PAYLOAD["body"], ticket_id="TCK-FAIL-REDIS")

    assert job_id in dead_letter_ids()  # RQ saw the failure instead of a fake success
    assert worker.get_job(job_id)["status"] == "failed"
    with session_scope() as s:
        assert s.get(Ticket, "TCK-FAIL-REDIS").resolution_status == "failed"


def test_redis_failure_with_retries_left_waits_queued_for_the_retry(monkeypatch):
    conn, q = _sync_redis(monkeypatch, is_async=True)
    from rq import SimpleWorker

    monkeypatch.setattr(worker, "run_ticket", _boom)
    _add_ticket("TCK-RETRY-REDIS")
    job_id = worker.enqueue(body=_PAYLOAD["body"], ticket_id="TCK-RETRY-REDIS")

    SimpleWorker([q], connection=conn).work(burst=True)

    j = worker.get_job(job_id)
    assert j["status"] == "queued"  # RQ scheduled a retry; not stuck 'running', not 'failed'
    assert "model backend down" in j["error"]
    assert q.fetch_job(job_id).get_status() == "scheduled"
    with session_scope() as s:
        assert s.get(Ticket, "TCK-RETRY-REDIS").resolution_status is None


def test_reconcile_leaves_jobs_rq_will_still_deliver(monkeypatch):
    _sync_redis(monkeypatch, is_async=True)  # nothing consumes the queue
    from agent_ops.reliability.reconcile import reconcile_orphans

    job_id = worker.enqueue(body=_PAYLOAD["body"], ticket_id="TCK-RQ-PENDING")

    assert job_id not in reconcile_orphans()  # re-dispatching would deliver it twice
    assert worker.get_job(job_id)["status"] == "queued"


def test_reconcile_marks_dead_lettered_jobs_failed(monkeypatch):
    conn, q = _sync_redis(monkeypatch, is_async=True)
    from agent_ops.reliability.reconcile import reconcile_orphans

    _add_ticket("TCK-RQ-DEAD")
    _add_job("JOB-RQ-DEAD", "running", "TCK-RQ-DEAD")
    rq_job = q.enqueue(_boom, job_id="JOB-RQ-DEAD")
    rq_job.set_status("failed")

    assert "JOB-RQ-DEAD" not in reconcile_orphans()
    assert worker.get_job("JOB-RQ-DEAD")["status"] == "failed"
    with session_scope() as s:
        assert s.get(Ticket, "TCK-RQ-DEAD").resolution_status == "failed"


def _rq_process_job_on(conn, q, job_id: str, ticket_id: str) -> None:
    from rq import SimpleWorker

    q.enqueue(
        "agent_ops.api.worker.process_job",
        job_id,
        _PAYLOAD["body"],
        ticket_id,
        None,
        None,
        job_id=job_id,
    )
    SimpleWorker([q], connection=conn).work(burst=True)


def test_rq_redelivery_does_not_race_a_live_execution(monkeypatch):
    conn, q = _sync_redis(monkeypatch, is_async=True)
    calls = []
    monkeypatch.setattr(worker, "run_ticket", lambda *a, **k: calls.append(a))
    _add_job("JOB-LIVE-DUP", "running", "TCK-LIVE-DUP")  # another worker is on it right now

    _rq_process_job_on(conn, q, "JOB-LIVE-DUP", "TCK-LIVE-DUP")

    assert calls == []
    assert worker.get_job("JOB-LIVE-DUP")["status"] == "running"


def test_rq_redelivery_resumes_an_execution_older_than_the_job_timeout(monkeypatch):
    from datetime import UTC, datetime, timedelta

    from agent_ops.api.queue_redis import JOB_TIMEOUT

    conn, q = _sync_redis(monkeypatch, is_async=True)
    stale = datetime.now(UTC) - timedelta(seconds=JOB_TIMEOUT + 30)
    _add_job("JOB-ABANDONED", "running", "TCK-ABANDONED", updated_at=stale)

    _rq_process_job_on(conn, q, "JOB-ABANDONED", "TCK-ABANDONED")

    assert worker.get_job("JOB-ABANDONED")["status"] == "succeeded"


class _FakeRQJob:
    def __init__(self, job_id: str, *, should_retry: bool):
        self.id = job_id
        self.should_retry = should_retry
        self.worker_name = "worker-1"


def test_rq_failure_callback_closes_a_job_whose_worker_was_killed():
    _add_ticket("TCK-KILLED")
    _add_job("JOB-KILLED", "running", "TCK-KILLED")

    worker.on_rq_failure(
        _FakeRQJob("JOB-KILLED", should_retry=False), None, RuntimeError, None, None
    )

    assert worker.get_job("JOB-KILLED")["status"] == "failed"
    with session_scope() as s:
        assert s.get(Ticket, "TCK-KILLED").resolution_status == "failed"


def test_rq_failure_callback_requeues_when_rq_will_retry():
    _add_job("JOB-KILLED-RETRY", "running", "TCK-KILLED-RETRY")

    worker.on_rq_failure(
        _FakeRQJob("JOB-KILLED-RETRY", should_retry=True), None, RuntimeError, None, None
    )

    assert worker.get_job("JOB-KILLED-RETRY")["status"] == "queued"


def test_rq_failure_callback_leaves_failures_process_job_recorded():
    _add_job("JOB-RECORDED", "failed", "TCK-RECORDED", error="RuntimeError: original")

    worker.on_rq_failure(
        _FakeRQJob("JOB-RECORDED", should_retry=False), None, RuntimeError, None, None
    )

    assert worker.get_job("JOB-RECORDED")["error"] == "RuntimeError: original"


def test_a_stale_failure_does_not_overwrite_a_newer_successful_run():
    from datetime import UTC, datetime, timedelta

    from agent_ops.backend.models import TraceRecord

    started = datetime.now(UTC) - timedelta(minutes=1)
    with session_scope() as s:
        s.add(TraceRecord(run_id="run-newer", ticket_id="TCK-STALE-FAIL", status="resolved"))
        s.add(
            Ticket(
                id="TCK-STALE-FAIL",
                body="x",
                status="resolved",
                resolution_status="resolved",
                run_id="run-newer",
            )
        )

    worker.mark_ticket_failed("TCK-STALE-FAIL", started)

    with session_scope() as s:
        assert s.get(Ticket, "TCK-STALE-FAIL").resolution_status == "resolved"
