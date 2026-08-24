"""Build a clean, known backend state for a single eval scenario.

Each scenario is self-contained: rather than depend on random seed picks, it
declares exactly the customer/orders/payments/subscription it needs, so the
expected final state is deterministic and precisely checkable.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlmodel import SQLModel

from agent_ops.backend.db import get_engine, init_db, session_scope
from agent_ops.backend.models import Customer, Order, Payment, Subscription

_now = lambda: datetime.now(UTC)  # noqa: E731


def reset_tables() -> None:
    eng = get_engine()
    SQLModel.metadata.drop_all(eng)
    init_db()


def _default_address() -> dict[str, Any]:
    return {
        "line1": "1 Old St",
        "city": "Portland",
        "region": "OR",
        "postal_code": "97201",
        "country": "US",
    }


def build_scenario_state(setup: dict[str, Any]) -> None:
    """Drop all tables and insert exactly the fixtures this scenario declares."""
    reset_tables()
    cust = setup.get("customer", {})
    cid = cust.get("id", "CUST-EVAL")

    with session_scope() as s:
        s.add(
            Customer(
                id=cid,
                name=cust.get("name", "Eval Customer"),
                email=cust.get("email", f"{cid.lower()}@example.com"),
                tier=cust.get("tier", "standard"),
                tenure_days=cust.get("tenure_days", 200),
                prefers_channel=cust.get("prefers_channel", "email"),
                identity_verified=cust.get("identity_verified", True),
            )
        )

        explicit_payments = setup.get("payments")
        pay_seq = 0
        for o in setup.get("orders", []):
            status = o.get("status", "delivered")
            total = float(o.get("total", 50.0))
            shipped = status in ("shipped", "delivered", "returned")
            order = Order(
                id=o["id"],
                customer_id=cid,
                status=status,
                total=total,
                items=o.get(
                    "items",
                    [{"sku": "AURORA-ITEM", "name": "Aurora Item", "qty": 1, "price": total}],
                ),
                shipping_address=o.get("shipping_address", _default_address()),
                carrier=o.get("carrier", "UPS" if shipped else None),
                tracking_number=o.get("tracking", "1Z9999999999" if shipped else None),
                placed_at=_now() - timedelta(days=o.get("days_ago", 5)),
                delivered_at=(_now() - timedelta(days=1))
                if status in ("delivered", "returned")
                else None,
                refunded_amount=float(o.get("refunded_amount", 0.0)),
            )
            s.add(order)
            if explicit_payments is None and status != "cancelled":
                pay_seq += 1
                s.add(
                    Payment(
                        id=f"PAY-E{pay_seq:04d}",
                        customer_id=cid,
                        order_id=o["id"],
                        amount=total,
                        status="succeeded",
                    )
                )

        if explicit_payments is not None:
            for i, p in enumerate(explicit_payments, start=1):
                s.add(
                    Payment(
                        id=p.get("id", f"PAY-X{i:04d}"),
                        customer_id=cid,
                        order_id=p.get("order_id"),
                        amount=float(p["amount"]),
                        status=p.get("status", "succeeded"),
                        is_duplicate=p.get("is_duplicate", False),
                    )
                )

        # Orders belonging to a *different* customer, for cross-customer /
        # scope-violation scenarios.
        for fo in setup.get("foreign_orders", []):
            owner = fo.get("owner", "CUST-FOREIGN")
            if s.get(Customer, owner) is None:
                s.add(Customer(id=owner, name="Other Person", email=f"{owner.lower()}@example.com"))
            f_status = fo.get("status", "delivered")
            f_total = float(fo.get("total", 50.0))
            s.add(
                Order(
                    id=fo["id"],
                    customer_id=owner,
                    status=f_status,
                    total=f_total,
                    shipping_address=_default_address(),
                    placed_at=_now() - timedelta(days=5),
                    refunded_amount=float(fo.get("refunded_amount", 0.0)),
                )
            )

        sub = setup.get("subscription")
        if sub:
            s.add(
                Subscription(
                    id=sub.get("id", "SUB-EVAL"),
                    customer_id=cid,
                    plan=sub.get("plan", "monthly-box"),
                    status=sub.get("status", "active"),
                    monthly_amount=float(sub.get("monthly_amount", 29.0)),
                    started_at=_now() - timedelta(days=120),
                    next_renewal=_now() + timedelta(days=10),
                )
            )
