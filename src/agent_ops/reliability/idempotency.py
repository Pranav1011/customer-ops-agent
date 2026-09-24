"""Idempotency for consequential writes.

A consequential write (refund, cancel, credit, ...) must execute *exactly once*
per logical request, even if the same ticket is re-delivered or a job is retried
after a crash. Each write is keyed by

    sha256(ticket_id | tool | canonical(args))

The first execution records its result; any later call with the same key replays
that result instead of acting again. Only *successful* writes are recorded — a
failed write leaves no key, so it stays retryable.

Scope: applied only when a ticket_id is present (i.e. a real inbound request).
Direct tool calls without a ticket are not deduplicated. Cross-process races are
resolved by the IdempotencyKey primary key; the durable queue hardens the
in-flight case.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from agent_ops.backend.db import session_scope
from agent_ops.backend.models import IdempotencyKey
from agent_ops.tools.registry import ToolResult


def idempotency_key(ticket_id: str | None, tool: str, args: dict[str, Any]) -> str:
    """Stable key over (ticket, tool, args). Args are canonicalized so key order
    and float formatting don't change the hash; distinct amounts (e.g. two legit
    partial refunds) produce distinct keys and both execute."""
    canonical = json.dumps(args, sort_keys=True, separators=(",", ":"), default=str)
    raw = f"{ticket_id or ''}|{tool}|{canonical}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def get_completed(key: str) -> ToolResult | None:
    """Return the stored result for an already-completed write, else None."""
    with session_scope() as s:
        row = s.get(IdempotencyKey, key)
        if row is None:
            return None
        return ToolResult(ok=True, data={**dict(row.result or {}), "idempotent_replay": True})


def record(key: str, tool: str, ticket_id: str | None, result: ToolResult) -> None:
    """Persist a successful write under its key. No-op on failure (retryable) or
    if another caller already recorded it (lost the race)."""
    if not result.ok:
        return
    with session_scope() as s:
        if s.get(IdempotencyKey, key) is not None:
            return
        s.add(IdempotencyKey(key=key, tool=tool, ticket_id=ticket_id, result=result.data))
