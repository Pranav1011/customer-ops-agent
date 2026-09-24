"""Crash recovery + dead-letter visibility for the durable queue.

If a worker dies mid-job, its Job row is left `running` (or `queued` if it was
never picked up). `reconcile_orphans()` — run on startup — re-dispatches those
jobs from their persisted payload so no inbound ticket is silently dropped.
Idempotency makes the re-run safe: an action that already completed is
replayed, not re-issued.

`dead_letter_ids()` surfaces jobs RQ gave up on after exhausting retries (its
FailedJobRegistry) so they can be inspected instead of vanishing.
"""

from __future__ import annotations

from sqlmodel import select

from agent_ops.backend.db import session_scope
from agent_ops.backend.models import Job

_UNFINISHED = ("queued", "running")


def reconcile_orphans() -> list[str]:
    """Re-queue every unfinished job from its stored payload. Returns the ids
    re-queued. Safe to call on every startup."""
    from agent_ops.api import worker

    with session_scope() as s:
        rows = [
            (j.id, dict(j.payload or {}), j.ticket_id)
            for j in s.exec(select(Job).where(Job.status.in_(_UNFINISHED))).all()  # type: ignore[attr-defined]
        ]
    for job_id, payload, ticket_id in rows:
        worker.requeue(job_id, payload, ticket_id)
    return [job_id for job_id, _, _ in rows]


def dead_letter_ids() -> list[str]:
    """Ids of jobs RQ failed permanently (dead-letter). Empty on the thread
    backend or if Redis is unreachable."""
    from agent_ops.config import get_settings

    if get_settings().queue_backend != "redis":
        return []
    try:
        from rq.registry import FailedJobRegistry

        from agent_ops.api.queue_redis import get_queue

        q = get_queue()
        return list(FailedJobRegistry(queue=q).get_job_ids())
    except Exception:
        return []


def peek_unfinished() -> list[str]:
    """Diagnostic: current unfinished job ids (used by tests + /metrics)."""
    with session_scope() as s:
        return [
            j.id
            for j in s.exec(select(Job).where(Job.status.in_(_UNFINISHED))).all()  # type: ignore[attr-defined]
        ]


__all__ = ["reconcile_orphans", "dead_letter_ids", "peek_unfinished"]
