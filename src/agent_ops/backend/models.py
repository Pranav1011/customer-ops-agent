"""SQLModel tables — the mock backend systems.

These are the domain-specific layer. Re-skinning Aurora to another domain means
rewriting this file, the seed generator, and the tools — nothing else.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, Column
from sqlmodel import Field, SQLModel


def _utcnow() -> datetime:
    return datetime.now(UTC)


class Customer(SQLModel, table=True):
    id: str = Field(primary_key=True)  # e.g. CUST-00042
    name: str
    email: str
    tier: str = Field(default="standard")  # standard | vip
    tenure_days: int = 0
    prefers_channel: str = Field(default="email")  # email | chat | phone
    identity_verified: bool = False
    notes: str = ""
    created_at: datetime = Field(default_factory=_utcnow)


class Order(SQLModel, table=True):
    id: str = Field(primary_key=True)  # e.g. ORD-000123
    customer_id: str = Field(foreign_key="customer.id", index=True)
    status: str = "placed"  # placed | packed | shipped | delivered | cancelled | returned
    total: float = 0.0
    currency: str = "USD"
    items: list[dict[str, Any]] = Field(default_factory=list, sa_column=Column(JSON))
    shipping_address: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    carrier: str | None = None
    tracking_number: str | None = None
    placed_at: datetime = Field(default_factory=_utcnow)
    delivered_at: datetime | None = None
    refunded_amount: float = 0.0


class Subscription(SQLModel, table=True):
    id: str = Field(primary_key=True)  # e.g. SUB-000045
    customer_id: str = Field(foreign_key="customer.id", index=True)
    plan: str = "monthly-box"
    status: str = "active"  # active | paused | cancelled
    monthly_amount: float = 0.0
    started_at: datetime = Field(default_factory=_utcnow)
    next_renewal: datetime | None = None
    cancelled_effective: datetime | None = None


class Payment(SQLModel, table=True):
    id: str = Field(primary_key=True)  # e.g. PAY-000789
    customer_id: str = Field(foreign_key="customer.id", index=True)
    order_id: str | None = Field(default=None, foreign_key="order.id", index=True)
    amount: float = 0.0
    status: str = "succeeded"  # succeeded | refunded | failed
    method: str = "card"
    is_duplicate: bool = False  # flags a genuine double-charge for that scenario
    created_at: datetime = Field(default_factory=_utcnow)


class Ticket(SQLModel, table=True):
    id: str = Field(primary_key=True)  # e.g. TCK-000321
    customer_id: str | None = Field(default=None, foreign_key="customer.id", index=True)
    channel: str = "email"
    subject: str = ""
    body: str = ""
    status: str = "open"  # open | resolved | escalated
    intent: str | None = None
    resolution_status: str | None = None  # resolved | escalated | failed
    run_id: str | None = None
    created_at: datetime = Field(default_factory=_utcnow)


class KBArticle(SQLModel, table=True):
    id: str = Field(primary_key=True)  # e.g. KB-012
    title: str = ""
    category: str = ""
    body: str = ""
    tags: list[str] = Field(default_factory=list, sa_column=Column(JSON))


class ActionLog(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    run_id: str = Field(index=True)
    ticket_id: str | None = Field(default=None, index=True)
    customer_id: str | None = None
    tool: str = ""
    args: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    result: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    ok: bool = True
    reversible: bool = True
    reversed: bool = False
    created_at: datetime = Field(default_factory=_utcnow)


class TraceRecord(SQLModel, table=True):
    run_id: str = Field(primary_key=True)
    ticket_id: str | None = Field(default=None, index=True)
    customer_id: str | None = None
    intent: str | None = None
    status: str | None = None  # resolved | escalated | failed
    path: str = ""  # path to the full JSON trace on disk
    summary: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=_utcnow)


class MemoryProfile(SQLModel, table=True):
    customer_id: str = Field(primary_key=True)
    semantic: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    episodic: list[dict[str, Any]] = Field(default_factory=list, sa_column=Column(JSON))
    updated_at: datetime = Field(default_factory=_utcnow)


class Escalation(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    ticket_id: str | None = Field(default=None, index=True)
    customer_id: str | None = None
    run_id: str | None = None
    reason: str = ""
    context: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    status: str = "open"  # open | resolved
    created_at: datetime = Field(default_factory=_utcnow)


class Job(SQLModel, table=True):
    """An asynchronous unit of work: resolve a ticket on the worker pool."""

    id: str = Field(primary_key=True)  # e.g. JOB-abc123
    ticket_id: str | None = Field(default=None, index=True)
    kind: str = "resolve"
    status: str = "queued"  # queued | running | succeeded | failed
    run_id: str | None = None
    error: str | None = None
    result: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)


ALL_TABLES = [
    Customer,
    Order,
    Subscription,
    Payment,
    Ticket,
    KBArticle,
    ActionLog,
    TraceRecord,
    MemoryProfile,
    Escalation,
    Job,
]
