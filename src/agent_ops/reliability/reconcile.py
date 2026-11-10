"""Crash recovery + dead-letter visibility for the durable queue.

If a worker dies mid-job, its Job row is left `running` (or `queued` if it was
never picked up). `reconcile_orphans()` — run on API startup — re-dispatches
those jobs from their persisted payload so no inbound ticket is silently
dropped. Idempotency makes the re-run safe: an action that already completed is
replayed, not re-issued.

On the thread backend every unfinished row at startup is an orphan: the pool
that held it died with the previous process. On the Redis backend a row can be
unfinished because RQ still holds the job, so reconcile asks RQ first: a job RQ
will still deliver is left alone, a job RQ dead-lettered is marked failed, and
only a job RQ has lost is re-dispatched.

`dead_letter_ids()` surfaces jobs RQ gave up on after exhausting retries (its
FailedJobRegistry) so they can be inspected instead of vanishing.
"""

from __future__ import annotations

from contextlib import suppress

from sqlmodel import select

from agent_ops.backend.db import session_scope
from agent_ops.backend.models import Job
from agent_ops.backend.transitions import IllegalTransition

_UNFINISHED = ("queued", "running")


def reconcile_orphans() -> list[str]:
    """Re-queue every orphaned unfinished job from its stored payload. Returns
    the ids re-queued. Safe to call on every startup."""
    from agent_ops.api import worker
    from agent_ops.config import get_settings

    with session_scope() as s:
        rows = [
            (j.id, dict(j.payload or {}), j.ticket_id)
            for j in s.exec(select(Job).where(Job.status.in_(_UNFINISHED))).all()  # type: ignore[attr-defined]
        ]
    held: set[str] | None = None
    if rows and get_settings().queue_backend == "redis":
        from agent_ops.api.queue_redis import delivery_state, held_job_ids

        held = held_job_ids()
    requeued = []
    for job_id, payload, ticket_id in rows:
        if held is not None:
            state = delivery_state(job_id, held)
            if state == "pending":
                continue
            if state == "dead":
                with suppress(IllegalTransition):  # a concurrent reconcile closed it
                    worker.mark_dead_lettered(job_id, ticket_id)
                continue
        if worker.requeue(job_id, payload, ticket_id):
            requeued.append(job_id)
    return requeued


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
