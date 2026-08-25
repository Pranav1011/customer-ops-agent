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

import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from functools import lru_cache
from typing import Any

from agent_ops.agent.graph import run_ticket
from agent_ops.backend.db import session_scope
from agent_ops.backend.models import Job
from agent_ops.config import get_settings


@lru_cache
def _executor() -> ThreadPoolExecutor:
    return ThreadPoolExecutor(
        max_workers=get_settings().worker_concurrency, thread_name_prefix="aurora-worker"
    )


def _now() -> datetime:
    return datetime.now(UTC)


def _set_status(job_id: str, **fields: Any) -> None:
    with session_scope() as s:
        job = s.get(Job, job_id)
        if job is None:
            return
        for k, v in fields.items():
            setattr(job, k, v)
        job.updated_at = _now()


def _process(
    job_id: str, body: str, ticket_id: str, customer_id: str | None, order_id: str | None
) -> None:
    _set_status(job_id, status="running")
    try:
        r = run_ticket(body, ticket_id=ticket_id, customer_id=customer_id, order_id=order_id)
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
        _set_status(job_id, status="succeeded", run_id=r["run_id"], result=result)
    except Exception as e:  # a job failure must not take down the pool
        _set_status(job_id, status="failed", error=f"{type(e).__name__}: {e}")


def enqueue(
    *, body: str, ticket_id: str, customer_id: str | None = None, order_id: str | None = None
) -> str:
    """Create a queued Job and submit it to the worker pool. Returns the job id."""
    job_id = f"JOB-{uuid.uuid4().hex[:8]}"
    with session_scope() as s:
        s.add(Job(id=job_id, ticket_id=ticket_id, status="queued"))
    _executor().submit(_process, job_id, body, ticket_id, customer_id, order_id)
    return job_id


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
