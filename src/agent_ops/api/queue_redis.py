"""Redis/RQ durable-queue backend. Selected via QUEUE_BACKEND=redis.

Unlike the in-process ThreadPool, jobs here survive process death: they live in
Redis until an RQ worker (`rq worker aurora`) runs
`agent_ops.api.worker.process_job`. Retries with backoff and a job timeout are
attached at enqueue time; exhausted/failed jobs land in RQ's FailedJobRegistry —
the dead-letter queue. Startup crash recovery lives in reliability/reconcile.py.

redis/rq are imported lazily so the default thread backend never touches them.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

from agent_ops.config import get_settings

QUEUE_NAME = "aurora"
JOB_TIMEOUT = 300  # seconds — a run exceeding this is failed, never hung forever


@lru_cache
def get_redis() -> Any:
    import redis

    return redis.Redis.from_url(get_settings().redis_url)


@lru_cache
def get_queue() -> Any:
    from rq import Queue

    return Queue(QUEUE_NAME, connection=get_redis(), default_timeout=JOB_TIMEOUT)


def enqueue_redis(
    job_id: str,
    body: str,
    ticket_id: str,
    customer_id: str | None,
    order_id: str | None,
) -> None:
    from rq import Retry

    get_queue().enqueue(
        "agent_ops.api.worker.process_job",
        job_id,
        body,
        ticket_id,
        customer_id,
        order_id,
        job_id=job_id,  # RQ job id == our Job id, so the two stay traceable
        retry=Retry(max=2, interval=[5, 15]),
        job_timeout=JOB_TIMEOUT,
        result_ttl=86_400,
    )
