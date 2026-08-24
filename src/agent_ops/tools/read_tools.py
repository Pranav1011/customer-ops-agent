"""Read tools — retrieve state from the mock backend. No side effects."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field
from sqlmodel import select

from agent_ops.backend import kb
from agent_ops.backend.db import session_scope
from agent_ops.backend.models import Customer, Order, Payment, Subscription, Ticket
from agent_ops.tools.registry import ToolContext, ToolResult, register


def _iso(dt: Any) -> str | None:
    return dt.isoformat() if dt is not None else None


def order_to_dict(o: Order) -> dict[str, Any]:
    return {
        "order_id": o.id,
        "customer_id": o.customer_id,
        "status": o.status,
        "total": o.total,
        "currency": o.currency,
        "items": o.items,
        "shipping_address": o.shipping_address,
        "carrier": o.carrier,
        "tracking_number": o.tracking_number,
        "placed_at": _iso(o.placed_at),
        "delivered_at": _iso(o.delivered_at),
        "refunded_amount": o.refunded_amount,
    }


def customer_to_dict(c: Customer) -> dict[str, Any]:
    return {
        "customer_id": c.id,
        "name": c.name,
        "email": c.email,
        "tier": c.tier,
        "tenure_days": c.tenure_days,
        "prefers_channel": c.prefers_channel,
        "identity_verified": c.identity_verified,
    }


def subscription_to_dict(s: Subscription) -> dict[str, Any]:
    return {
        "subscription_id": s.id,
        "customer_id": s.customer_id,
        "plan": s.plan,
        "status": s.status,
        "monthly_amount": s.monthly_amount,
        "started_at": _iso(s.started_at),
        "next_renewal": _iso(s.next_renewal),
        "cancelled_effective": _iso(s.cancelled_effective),
    }


def payment_to_dict(p: Payment) -> dict[str, Any]:
    return {
        "payment_id": p.id,
        "customer_id": p.customer_id,
        "order_id": p.order_id,
        "amount": p.amount,
        "status": p.status,
        "method": p.method,
        "is_duplicate": p.is_duplicate,
        "created_at": _iso(p.created_at),
    }


# --- get_order ---
class GetOrderArgs(BaseModel):
    order_id: str = Field(description="The order id, e.g. ORD-000123")


@register(
    name="get_order",
    description="Fetch a single order by id: status, items, total, shipping address, carrier and tracking.",
    kind="read",
    args_model=GetOrderArgs,
)
def get_order(ctx: ToolContext, args: GetOrderArgs) -> ToolResult:
    with session_scope() as s:
        o = s.get(Order, args.order_id)
        if o is None:
            return ToolResult(ok=False, error=f"not_found: order {args.order_id}")
        return ToolResult(ok=True, data=order_to_dict(o))


# --- find_orders ---
class FindOrdersArgs(BaseModel):
    customer_id: str = Field(description="Customer id, e.g. CUST-00042")
    limit: int = Field(default=10, ge=1, le=50)


@register(
    name="find_orders",
    description="List a customer's orders (most recent first).",
    kind="read",
    args_model=FindOrdersArgs,
)
def find_orders(ctx: ToolContext, args: FindOrdersArgs) -> ToolResult:
    with session_scope() as s:
        rows = s.exec(
            select(Order)
            .where(Order.customer_id == args.customer_id)
            .order_by(Order.placed_at.desc())
            .limit(args.limit)
        ).all()
        return ToolResult(
            ok=True, data={"orders": [order_to_dict(o) for o in rows], "count": len(rows)}
        )


# --- get_customer ---
class GetCustomerArgs(BaseModel):
    customer_id: str = Field(description="Customer id, e.g. CUST-00042")


@register(
    name="get_customer",
    description="Fetch a customer's profile: name, email, tier, tenure, preferred channel, and whether identity is verified.",
    kind="read",
    args_model=GetCustomerArgs,
)
def get_customer(ctx: ToolContext, args: GetCustomerArgs) -> ToolResult:
    with session_scope() as s:
        c = s.get(Customer, args.customer_id)
        if c is None:
            return ToolResult(ok=False, error=f"not_found: customer {args.customer_id}")
        return ToolResult(ok=True, data=customer_to_dict(c))


# --- get_customer_history ---
class GetCustomerHistoryArgs(BaseModel):
    customer_id: str = Field(description="Customer id, e.g. CUST-00042")


@register(
    name="get_customer_history",
    description="Summarize a customer's history: order counts by status, recent orders, subscriptions, and past tickets.",
    kind="read",
    args_model=GetCustomerHistoryArgs,
)
def get_customer_history(ctx: ToolContext, args: GetCustomerHistoryArgs) -> ToolResult:
    with session_scope() as s:
        c = s.get(Customer, args.customer_id)
        if c is None:
            return ToolResult(ok=False, error=f"not_found: customer {args.customer_id}")
        orders = s.exec(
            select(Order)
            .where(Order.customer_id == args.customer_id)
            .order_by(Order.placed_at.desc())
        ).all()
        subs = s.exec(
            select(Subscription).where(Subscription.customer_id == args.customer_id)
        ).all()
        tickets = s.exec(
            select(Ticket)
            .where(Ticket.customer_id == args.customer_id)
            .order_by(Ticket.created_at.desc())
            .limit(10)
        ).all()
        status_counts: dict[str, int] = {}
        for o in orders:
            status_counts[o.status] = status_counts.get(o.status, 0) + 1
        return ToolResult(
            ok=True,
            data={
                "customer": customer_to_dict(c),
                "order_count": len(orders),
                "orders_by_status": status_counts,
                "recent_orders": [order_to_dict(o) for o in orders[:5]],
                "subscriptions": [subscription_to_dict(x) for x in subs],
                "past_tickets": [
                    {
                        "ticket_id": t.id,
                        "subject": t.subject,
                        "status": t.status,
                        "intent": t.intent,
                    }
                    for t in tickets
                ],
            },
        )


# --- get_subscription ---
class GetSubscriptionArgs(BaseModel):
    customer_id: str = Field(description="Customer id, e.g. CUST-00042")


@register(
    name="get_subscription",
    description="Fetch a customer's subscription(s): plan, status, monthly amount, next renewal.",
    kind="read",
    args_model=GetSubscriptionArgs,
)
def get_subscription(ctx: ToolContext, args: GetSubscriptionArgs) -> ToolResult:
    with session_scope() as s:
        subs = s.exec(
            select(Subscription).where(Subscription.customer_id == args.customer_id)
        ).all()
        if not subs:
            return ToolResult(ok=True, data={"subscriptions": [], "count": 0})
        return ToolResult(
            ok=True,
            data={"subscriptions": [subscription_to_dict(x) for x in subs], "count": len(subs)},
        )


# --- get_payment_history ---
class GetPaymentHistoryArgs(BaseModel):
    customer_id: str = Field(description="Customer id, e.g. CUST-00042")
    order_id: str | None = Field(default=None, description="Optionally filter to one order")


@register(
    name="get_payment_history",
    description="List a customer's payments, optionally filtered to a single order. Useful for spotting duplicate/double charges.",
    kind="read",
    args_model=GetPaymentHistoryArgs,
)
def get_payment_history(ctx: ToolContext, args: GetPaymentHistoryArgs) -> ToolResult:
    with session_scope() as s:
        stmt = select(Payment).where(Payment.customer_id == args.customer_id)
        if args.order_id:
            stmt = stmt.where(Payment.order_id == args.order_id)
        rows = s.exec(stmt.order_by(Payment.created_at.asc())).all()
        return ToolResult(
            ok=True, data={"payments": [payment_to_dict(p) for p in rows], "count": len(rows)}
        )


# --- search_knowledge_base ---
class SearchKBArgs(BaseModel):
    query: str = Field(description="Natural-language search over Aurora policy articles")
    k: int = Field(default=3, ge=1, le=8)


@register(
    name="search_knowledge_base",
    description="Semantic search over Aurora's policy knowledge base. Use to ground decisions in policy (refund thresholds, escalation rules, etc.).",
    kind="read",
    args_model=SearchKBArgs,
)
def search_knowledge_base(ctx: ToolContext, args: SearchKBArgs) -> ToolResult:
    hits = kb.search(args.query, k=args.k)
    return ToolResult(ok=True, data={"results": hits, "count": len(hits)})


# --- get_customer_memory ---
class GetMemoryArgs(BaseModel):
    customer_id: str = Field(description="Customer id, e.g. CUST-00042")


@register(
    name="get_customer_memory",
    description="Recall long-term memory for a customer: durable semantic facts (tier, VIP, preferred channel) and recent past resolutions (episodic).",
    kind="read",
    args_model=GetMemoryArgs,
)
def get_customer_memory(ctx: ToolContext, args: GetMemoryArgs) -> ToolResult:
    from agent_ops.memory.long_term import recall

    return ToolResult(ok=True, data=recall(args.customer_id))
