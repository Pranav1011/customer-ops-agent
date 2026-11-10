"""Async job queue: a bounded worker pool that resolves tickets off the request
path.

Agent runs are slow and variable (a local LLM can take ~40s), so submitting a
ticket must not block the HTTP request. A ticket submission enqueues a `Job`,
returns immediately, and a `ThreadPoolExecutor` of size `worker_concurrency`
processes jobs; clients poll `GET /jobs/{id}`. The pool size bounds concurrency
(backpressure). In production this becomes a durable queue (Redis/SQS) with
dedicated worker processes — the interface here is intentionally the same shape.
"""

from __future__ import annotations

import logging
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from typing import Any

from sqlalchemy import update

from agent_ops.agent.graph import run_ticket
from agent_ops.backend.db import session_scope
from agent_ops.backend.models import Job, Ticket, TraceRecord
from agent_ops.backend.transitions import (
    JOB_TERMINAL,
    JOB_TRANSITIONS,
    IllegalTransition,
    check_transition,
)
from agent_ops.config import get_settings

log = logging.getLogger(__name__)


@lru_cache
def _executor() -> ThreadPoolExecutor:
    return ThreadPoolExecutor(
        max_workers=get_settings().worker_concurrency, thread_name_prefix="aurora-worker"
    )


def _now() -> datetime:
    return datetime.now(UTC)


def _transition(job_id: str, target: str, **fields: Any) -> None:
    """Move a job to `target`, refusing moves JOB_TRANSITIONS does not allow.

    The write is a compare-and-set on the status read, so two deliveries racing
    on one job cannot both win the same move."""
    with session_scope() as s:
        job = s.get(Job, job_id)
        if job is None:
            return
        current = job.status
        check_transition(JOB_TRANSITIONS, "job", job_id, current, target)
        moved = s.execute(
            update(Job)
            .where(Job.id == job_id, Job.status == current)  # type: ignore[arg-type]
            .values(status=target, updated_at=_now(), **fields)
        ).rowcount
        if moved != 1:
            raise IllegalTransition(
                f"conflict: job {job_id} left {current} before it could move to {target}"
            )


def _rq_job() -> Any:
    """The RQ job this call is running under, or None on the thread backend."""
    if get_settings().queue_backend != "redis":
        return None
    from rq import get_current_job

    return get_current_job()


def _as_utc(ts: datetime) -> datetime:
    """SQLite hands datetimes back naive; they were written in UTC."""
    return ts if ts.tzinfo is not None else ts.replace(tzinfo=UTC)


def _claim(job_id: str) -> bool:
    """Move a delivered job to running. False means this delivery must not run it.

    A job already succeeded or failed is a duplicate delivery and is skipped. A
    job already running is resumed only under RQ and only once its execution is
    older than the RQ job timeout: by then RQ has killed that execution, so this
    is RQ re-delivering an abandoned job, not a second copy racing a live one."""
    with session_scope() as s:
        job = s.get(Job, job_id)
        if job is None:
            return True
        status, updated_at = job.status, job.updated_at
    if status in JOB_TERMINAL:
        return False
    if status == "running":
        if _rq_job() is None:
            return False
        from agent_ops.api.queue_redis import JOB_TIMEOUT

        if _now() - _as_utc(updated_at) < timedelta(seconds=JOB_TIMEOUT):
            return False
        with session_scope() as s:  # compare-and-set, so only one re-delivery resumes it
            moved = s.execute(
                update(Job)
                .where(Job.id == job_id, Job.status == "running", Job.updated_at == updated_at)  # type: ignore[arg-type]
                .values(updated_at=_now())
            ).rowcount
        return moved == 1
    try:
        _transition(job_id, "running")
    except IllegalTransition:
        return False  # another delivery claimed it first
    return True


def mark_ticket_failed(ticket_id: str | None, started_at: datetime | None = None) -> None:
    """Record that the latest run of a ticket failed, so it doesn't sit 'open'.

    A run that finished after `started_at` (a newer re-run of the same ticket)
    already owns the ticket's status, so a stale failure leaves it alone."""
    if not ticket_id:
        return
    with session_scope() as s:
        t = s.get(Ticket, ticket_id)
        if t is None:
            return
        if started_at is not None and t.run_id:
            latest = s.get(TraceRecord, t.run_id)
            if latest is not None and _as_utc(latest.created_at) >= started_at:
                return
        t.resolution_status = "failed"


def _record_failure(
    job_id: str, ticket_id: str | None, error: str, rq_job: Any, started_at: datetime | None
) -> None:
    """Mark the job failed, or back to queued while RQ still has a retry for it
    (a sync RQ queue never retries)."""
    if rq_job is not None and rq_job.should_retry and rq_job.worker_name != "sync":
        _transition(job_id, "queued", error=error)
        return
    _transition(job_id, "failed", error=error)
    mark_ticket_failed(ticket_id, started_at)


def mark_dead_lettered(job_id: str, ticket_id: str | None) -> None:
    """RQ gave up on this job without our worker recording it (e.g. its last
    worker was killed): close it as failed instead of leaving it unfinished."""
    _transition(job_id, "failed", error="dead-lettered by the queue")
    mark_ticket_failed(ticket_id)


def on_rq_failure(rq_job: Any, connection: Any, exc_type: Any, exc: Any, tb: Any) -> None:
    """RQ failure callback. process_job records its own failures, so this only
    acts on a job RQ failed without it: an execution whose worker was killed
    (RQ's abandoned-job cleanup). Such a job would otherwise stay 'running'.
    It never raises, because RQ runs it inside its registry cleanup."""
    try:
        with session_scope() as s:
            job = s.get(Job, rq_job.id)
            if job is None or job.status != "running":
                return
            ticket_id = job.ticket_id
        error = f"{getattr(exc_type, '__name__', 'Error')}: worker lost mid-run"
        _record_failure(rq_job.id, ticket_id, error, rq_job, None)
    except Exception:
        log.exception("could not record RQ failure for job %s", getattr(rq_job, "id", "?"))


def process_job(
    job_id: str, body: str, ticket_id: str, customer_id: str | None, order_id: str | None
) -> None:
    """Run one ticket to completion and record the outcome on its Job row.

    Module-level and importable by name so an out-of-process RQ worker can run it
    (`agent_ops.api.worker.process_job`); the in-process ThreadPool backend calls
    it directly. Idempotency makes a re-run after a crash safe.

    On failure the job is recorded and, under RQ, the exception is re-raised so
    RQ's retry and dead-letter handling see it. The thread pool has no retry, so
    there the failure ends with the recorded status."""
    started_at = _now()
    if not _claim(job_id):
        return
    try:
        r = run_ticket(body, ticket_id=ticket_id, customer_id=customer_id, order_id=order_id)
    except Exception as e:  # a job failure must not take down the pool
        rq_job = _rq_job()
        _record_failure(job_id, ticket_id, f"{type(e).__name__}: {e}", rq_job, started_at)
        if rq_job is not None:
            raise
        return
    result = {
        "ticket_id": ticket_id,
        "run_id": r["run_id"],
        "intent": r["intent"],
        "status": r["status"],
        "escalated": r["escalated"],
        "customer_reply": r["customer_reply"],
        "escalation_reason": r["escalation_reason"],
        "summary": r["summary"],
    }
    try:
        _transition(job_id, "succeeded", run_id=r["run_id"], result=result, error=None)
    except IllegalTransition:
        # The ticket ran; only the bookkeeping lost a race. Raising here would
        # make RQ retry, i.e. run the whole ticket again.
        log.warning("job %s finished but its status had moved; result not recorded", job_id)


def enqueue(
    *, body: str, ticket_id: str, customer_id: str | None = None, order_id: str | None = None
) -> str:
    """Create a queued Job and submit it to the worker pool. Returns the job id."""
    job_id = f"JOB-{uuid.uuid4().hex[:8]}"
    payload = {"body": body, "customer_id": customer_id, "order_id": order_id}
    with session_scope() as s:
        s.add(Job(id=job_id, ticket_id=ticket_id, status="queued", payload=payload))
    _dispatch(job_id, body, ticket_id, customer_id, order_id)
    return job_id


def _dispatch(
    job_id: str, body: str, ticket_id: str, customer_id: str | None, order_id: str | None
) -> None:
    """Hand a job to the configured backend: the durable Redis/RQ queue, or the
    in-process thread pool."""
    if get_settings().queue_backend == "redis":
        from agent_ops.api.queue_redis import enqueue_redis

        enqueue_redis(job_id, body, ticket_id, customer_id, order_id)
    else:
        _executor().submit(process_job, job_id, body, ticket_id, customer_id, order_id)


def requeue(job_id: str, payload: dict[str, Any], ticket_id: str | None) -> bool:
    """Re-dispatch an existing crash-orphaned job under its own id, from the
    payload persisted at enqueue time. Idempotency keeps the re-run safe.
    Returns False if a concurrent reconcile already re-queued it."""
    with session_scope() as s:
        job = s.get(Job, job_id)
        status = job.status if job is not None else None
    if status is not None and status != "queued":
        try:
            _transition(job_id, "queued")
        except IllegalTransition:
            return False  # a concurrent reconcile (API and worker both run one) got it first
    _dispatch(
        job_id,
        str(payload.get("body", "")),
        ticket_id or "",
        payload.get("customer_id"),
        payload.get("order_id"),
    )
    return True


def get_job(job_id: str) -> dict[str, Any] | None:
    with session_scope() as s:
        job = s.get(Job, job_id)
        if job is None:
            return None
        return {
            "job_id": job.id,
            "ticket_id": job.ticket_id,
            "status": job.status,
            "run_id": job.run_id,
            "error": job.error,
            "result": job.result or None,
            "created_at": job.created_at.isoformat(),
            "updated_at": job.updated_at.isoformat(),
        }
