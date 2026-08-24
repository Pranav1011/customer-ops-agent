"""HTTP routes."""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from sqlmodel import select

from agent_ops.agent.graph import run_ticket
from agent_ops.backend.db import session_scope
from agent_ops.backend.models import Escalation, Ticket, TraceRecord

router = APIRouter()


class SubmitTicket(BaseModel):
    body: str = Field(description="The customer's message")
    customer_id: str | None = None
    order_id: str | None = None
    channel: str = "email"
    subject: str = ""


class TicketResult(BaseModel):
    ticket_id: str
    run_id: str
    intent: str | None
    status: str | None
    escalated: bool
    customer_reply: str
    escalation_reason: str | None
    summary: dict[str, Any]


@router.post("/tickets", response_model=TicketResult)
def submit_ticket(payload: SubmitTicket) -> TicketResult:
    ticket_id = f"TCK-{uuid.uuid4().hex[:8].upper()}"
    with session_scope() as s:
        s.add(
            Ticket(
                id=ticket_id,
                customer_id=payload.customer_id,
                channel=payload.channel,
                subject=payload.subject,
                body=payload.body,
                status="open",
            )
        )

    result = run_ticket(
        payload.body,
        ticket_id=ticket_id,
        customer_id=payload.customer_id,
        order_id=payload.order_id,
    )
    return TicketResult(
        ticket_id=ticket_id,
        run_id=result["run_id"],
        intent=result["intent"],
        status=result["status"],
        escalated=result["escalated"],
        customer_reply=result["customer_reply"],
        escalation_reason=result["escalation_reason"],
        summary=result["summary"],
    )


@router.get("/tickets/{ticket_id}")
def get_ticket(ticket_id: str) -> dict[str, Any]:
    with session_scope() as s:
        t = s.get(Ticket, ticket_id)
        if t is None:
            raise HTTPException(404, f"ticket not found: {ticket_id}")
        return {
            "ticket_id": t.id,
            "customer_id": t.customer_id,
            "channel": t.channel,
            "subject": t.subject,
            "body": t.body,
            "status": t.status,
            "intent": t.intent,
            "resolution_status": t.resolution_status,
            "run_id": t.run_id,
            "created_at": t.created_at.isoformat(),
        }


@router.get("/queue")
def list_queue(status: str | None = None, limit: int = 50) -> dict[str, Any]:
    with session_scope() as s:
        stmt = select(Ticket).order_by(Ticket.created_at.desc()).limit(limit)
        if status:
            stmt = (
                select(Ticket)
                .where(Ticket.status == status)
                .order_by(Ticket.created_at.desc())
                .limit(limit)
            )
        rows = s.exec(stmt).all()
        return {
            "count": len(rows),
            "tickets": [
                {
                    "ticket_id": t.id,
                    "customer_id": t.customer_id,
                    "subject": t.subject,
                    "status": t.status,
                    "intent": t.intent,
                    "resolution_status": t.resolution_status,
                    "run_id": t.run_id,
                }
                for t in rows
            ],
        }


@router.get("/trace/{run_id}")
def get_trace(run_id: str) -> dict[str, Any]:
    with session_scope() as s:
        rec = s.get(TraceRecord, run_id)
        if rec is None:
            raise HTTPException(404, f"trace not found: {run_id}")
        path = Path(rec.path)
    if not path.exists():
        raise HTTPException(410, "trace file missing on disk")
    return json.loads(path.read_text(encoding="utf-8"))


@router.get("/customers/{customer_id}/memory")
def get_customer_memory(customer_id: str) -> dict[str, Any]:
    from agent_ops.memory.long_term import load_profile

    return load_profile(customer_id)


@router.get("/escalations")
def list_escalations(status: str = "open", limit: int = 50) -> dict[str, Any]:
    with session_scope() as s:
        rows = s.exec(
            select(Escalation)
            .where(Escalation.status == status)
            .order_by(Escalation.created_at.desc())
            .limit(limit)
        ).all()
        return {
            "count": len(rows),
            "escalations": [
                {
                    "id": e.id,
                    "ticket_id": e.ticket_id,
                    "customer_id": e.customer_id,
                    "run_id": e.run_id,
                    "reason": e.reason,
                    "context": e.context,
                    "status": e.status,
                    "created_at": e.created_at.isoformat(),
                }
                for e in rows
            ],
        }
