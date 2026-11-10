"""Legal status transitions for the rows that carry a lifecycle.

Each table maps a status to the set of statuses it may move to next. All status
writes go through `transition_error` / `check_transition`, so an illegal move is
refused in one place with a message an agent (or a human) can act on: it names
the record, its current state, the requested state and the legal next states.
"""

from __future__ import annotations

# queued -> running -> succeeded | failed. running -> queued is crash recovery or
# a pending RQ retry; queued -> failed is a job RQ dead-lettered before it ran;
# failed -> queued is a manual retry.
JOB_TRANSITIONS: dict[str, set[str]] = {
    "queued": {"running", "failed"},
    "running": {"succeeded", "failed", "queued"},
    "succeeded": set(),
    "failed": {"queued"},
}
# A delivery that finds the job in one of these states must not run it again.
JOB_TERMINAL = frozenset({"succeeded", "failed"})

SUBSCRIPTION_TRANSITIONS: dict[str, set[str]] = {
    "active": {"paused", "cancelled"},
    "paused": {"active", "cancelled"},
    "cancelled": set(),
}


class IllegalTransition(ValueError):
    """Raised when a status write is not in the transition table."""


def transition_error(
    table: dict[str, set[str]], kind: str, ident: str, current: str, target: str
) -> str | None:
    """Return None if `current -> target` is legal, else a `conflict:` message."""
    legal = table.get(current, set())
    if target in legal:
        return None
    legal_next = ", ".join(sorted(legal)) or "none"
    if target == current:
        return f"conflict: {kind} {ident} is already {current} (legal next: {legal_next})"
    return (
        f"conflict: {kind} {ident} is {current}; cannot move to {target} (legal next: {legal_next})"
    )


def check_transition(
    table: dict[str, set[str]], kind: str, ident: str, current: str, target: str
) -> None:
    """Raise IllegalTransition unless `current -> target` is legal."""
    err = transition_error(table, kind, ident, current, target)
    if err is not None:
        raise IllegalTransition(err)
