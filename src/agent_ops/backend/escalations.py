"""Escalation lifecycle: open -> resolved, at most one open row per ticket.

A ticket that escalates on several runs (a re-run, or the escalate tool plus the
run's own escalation) keeps one open escalation that points at the latest run.
Only a human closes it: a later run that resolves the ticket leaves it open.
"""

from __future__ import annotations

from typing import Any

from sqlmodel import Session, select

from agent_ops.backend.models import Escalation


def open_escalation(
    s: Session,
    *,
    ticket_id: str | None,
    customer_id: str | None,
    run_id: str | None,
    reason: str,
    context: dict[str, Any],
) -> Escalation:
    """Return the ticket's open escalation, refreshed to this run, or open one.

    Only an escalation raised for the same customer is reused, so a call naming
    some other customer's ticket can never rewrite that customer's escalation."""
    existing = None
    if ticket_id is not None:
        existing = s.exec(
            select(Escalation).where(
                Escalation.ticket_id == ticket_id,
                Escalation.status == "open",
                Escalation.customer_id == customer_id,
            )
        ).first()
    if existing is not None:
        existing.run_id = run_id
        existing.reason = reason
        existing.context = context
        s.add(existing)
        s.flush()
        return existing
    esc = Escalation(
        ticket_id=ticket_id,
        customer_id=customer_id,
        run_id=run_id,
        reason=reason,
        context=context,
        status="open",
    )
    s.add(esc)
    s.flush()
    return esc
