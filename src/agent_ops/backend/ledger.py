"""Money facts derived from the payment ledger, shared by the refund tool and
the policy engine so both cap a refund the same way."""

from __future__ import annotations

from sqlalchemy import func
from sqlmodel import Session, select

from agent_ops.backend.models import Order, Payment


def paid_amount(s: Session, order_id: str) -> float:
    """What the customer actually paid on an order: its succeeded payments,
    including a genuine double charge. Refund rows are recorded separately."""
    total = s.exec(
        select(func.coalesce(func.sum(Payment.amount), 0.0)).where(
            Payment.order_id == order_id, Payment.status == "succeeded"
        )
    ).one()
    return round(float(total), 2)


def refundable_remaining(s: Session, order: Order) -> tuple[float, float]:
    """(paid, remaining): a refund may return what was paid, minus what has
    already been refunded. An order that was never paid has nothing to refund."""
    paid = paid_amount(s, order.id)
    return paid, round(max(paid - order.refunded_amount, 0.0), 2)
