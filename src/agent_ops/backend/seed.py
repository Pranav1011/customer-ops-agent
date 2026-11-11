"""Deterministic seed generator for the Aurora mock backend.

Run: `make seed` (or `python -m agent_ops.backend.seed`). Rebuilds the SQLite
database and the Chroma KB from a fixed random seed so results are reproducible.
"""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta

from faker import Faker
from sqlmodel import SQLModel

from agent_ops.backend import kb
from agent_ops.backend.db import get_engine, init_db, session_scope
from agent_ops.backend.kb_content import KB_ARTICLES
from agent_ops.backend.models import (
    Customer,
    Order,
    Payment,
    Subscription,
    Ticket,
)

SEED = 42
N_CUSTOMERS = 300

PRODUCTS = [
    ("Aurora Weighted Blanket", 89.0),
    ("Aurora Ceramic Diffuser", 42.0),
    ("Aurora Linen Sheet Set", 129.0),
    ("Aurora Wellness Tea Box", 24.0),
    ("Aurora Sunrise Alarm", 68.0),
    ("Aurora Cast-Iron Kettle", 74.0),
    ("Aurora Merino Throw", 115.0),
    ("Aurora Scented Candle Trio", 38.0),
    ("Aurora Bath Salt Set", 29.0),
    ("Aurora Desk Plant Kit", 33.0),
]

CARRIERS = ["UPS", "FedEx", "USPS", "DHL"]


# The console's sample tickets use the first few customers, so the queue's demo
# tickets are drawn from later ones and the two never refund the same order.
_DEMO_FIRST_CUSTOMER = 10


def _demo_tickets(
    customers: list[Customer],
    orders: list[Order],
    payments: list[Payment],
    subscriptions: list[Subscription],
) -> list[Ticket]:
    """Open tickets for the console's queue, each naming a real seeded record so
    the agent can act on it. Together they show both outcomes: four resolve with
    an action (a tracking answer, an in-policy refund, a duplicate-charge refund,
    an address change) and two stop for a human (a cancellation, which policy
    always routes to a person, and a refund over the auto-approval threshold)."""
    pool = {c.id: c for c in customers[_DEMO_FIRST_CUSTOMER:]}
    used: set[str] = set()

    def pick_order(match, *, verified: bool = False) -> Order:
        for o in orders:
            c = pool.get(o.customer_id)
            if c is None or c.id in used or (verified and not c.identity_verified):
                continue
            if match(o):
                used.add(c.id)
                return o
        raise RuntimeError("seed has no order for a demo ticket")

    def has_item(o: Order, name: str) -> bool:
        return any(i["name"] == name for i in o.items)

    paid = {p.order_id for p in payments if not p.is_duplicate}
    shipped = pick_order(lambda o: o.status == "shipped")
    stale_tea = pick_order(
        lambda o: (
            o.status == "delivered"
            and o.id in paid
            and o.total <= 100
            and has_item(o, "Aurora Wellness Tea Box")
        )
    )
    dup = next(
        p
        for p in payments
        if p.is_duplicate
        and p.amount <= 100
        and p.customer_id in pool
        and p.customer_id not in used
    )
    used.add(dup.customer_id)
    unshipped = pick_order(lambda o: o.status in ("placed", "packed"), verified=True)
    torn = pick_order(
        lambda o: (
            o.status == "delivered"
            and o.id in paid
            and o.total > 100
            and has_item(o, "Aurora Weighted Blanket")
        )
    )
    sub = next(
        x
        for x in subscriptions
        if x.status == "active" and x.customer_id in pool and x.customer_id not in used
    )

    specs = [
        (
            shipped.customer_id,
            "Where is my order?",
            f"Hi, where is my order {shipped.id}? I ordered a while ago and it still hasn't "
            "arrived.",
        ),
        (
            stale_tea.customer_id,
            "Refund for a stale tea box",
            f"The tea box in order {stale_tea.id} tastes stale. I'd like a refund, please.",
        ),
        (
            sub.customer_id,
            "Cancel my subscription",
            "Please cancel my subscription. The monthly box is too much for me right now.",
        ),
        (
            dup.customer_id,
            "Charged twice",
            f"I was charged twice for order {dup.order_id}. Can you check and refund the extra "
            "charge?",
        ),
        (
            unshipped.customer_id,
            "Change shipping address",
            f"I moved. Can you change the shipping address on order {unshipped.id} before it "
            "ships?",
        ),
        (
            torn.customer_id,
            "Blanket arrived damaged",
            f"The weighted blanket in order {torn.id} arrived torn at the seam. Very disappointed.",
        ),
    ]
    by_id = {c.id: c for c in customers}
    return [
        Ticket(
            id=f"TCK-{idx:06d}",
            customer_id=cid,
            channel=by_id[cid].prefers_channel,
            subject=subject,
            body=body,
            status="open",
        )
        for idx, (cid, subject, body) in enumerate(specs, start=1)
    ]


def generate() -> dict[str, int]:
    fake = Faker()
    Faker.seed(SEED)
    rng = random.Random(SEED)
    now = datetime.now(UTC)

    customers: list[Customer] = []
    orders: list[Order] = []
    subscriptions: list[Subscription] = []
    payments: list[Payment] = []

    order_seq = 0
    pay_seq = 0
    sub_seq = 0

    for i in range(1, N_CUSTOMERS + 1):
        cid = f"CUST-{i:05d}"
        tenure = rng.randint(20, 1500)
        tier = "vip" if rng.random() < 0.12 else "standard"
        cust = Customer(
            id=cid,
            name=fake.name(),
            email=fake.unique.email(),
            tier=tier,
            tenure_days=tenure,
            prefers_channel=rng.choice(["email", "email", "chat", "phone"]),
            # Most established customers are verified; a chunk are not (drives
            # identity-verification guardrail scenarios).
            identity_verified=rng.random() < 0.7,
            notes="",
            created_at=now - timedelta(days=tenure),
        )
        customers.append(cust)

        n_orders = rng.randint(1, 15)
        for _ in range(n_orders):
            order_seq += 1
            oid = f"ORD-{order_seq:06d}"
            n_items = rng.randint(1, 3)
            items = []
            total = 0.0
            for _ in range(n_items):
                name, price = rng.choice(PRODUCTS)
                qty = rng.randint(1, 2)
                items.append(
                    {
                        "sku": name.replace(" ", "-").upper(),
                        "name": name,
                        "qty": qty,
                        "price": price,
                    }
                )
                total += price * qty
            total = round(total, 2)

            placed_days_ago = rng.randint(0, 400)
            placed_at = now - timedelta(days=placed_days_ago)
            status = rng.choices(
                ["placed", "packed", "shipped", "delivered", "cancelled", "returned"],
                weights=[8, 8, 20, 55, 5, 4],
            )[0]

            carrier = tracking = None
            delivered_at = None
            if status in ("shipped", "delivered", "returned"):
                carrier = rng.choice(CARRIERS)
                tracking = f"1Z{rng.randint(10**9, 10**10 - 1)}"
            if status in ("delivered", "returned"):
                delivered_at = placed_at + timedelta(days=rng.randint(2, 7))

            addr = {
                "line1": fake.street_address(),
                "city": fake.city(),
                "region": fake.state_abbr(),
                "postal_code": fake.postcode(),
                "country": "US",
            }
            order = Order(
                id=oid,
                customer_id=cid,
                status=status,
                total=total,
                currency="USD",
                items=items,
                shipping_address=addr,
                carrier=carrier,
                tracking_number=tracking,
                placed_at=placed_at,
                delivered_at=delivered_at,
                refunded_amount=0.0,
            )
            orders.append(order)

            # One successful payment per order (unless cancelled pre-payment sometimes).
            if status != "cancelled" or rng.random() < 0.5:
                pay_seq += 1
                payments.append(
                    Payment(
                        id=f"PAY-{pay_seq:06d}",
                        customer_id=cid,
                        order_id=oid,
                        amount=total,
                        status="succeeded",
                        method="card",
                        is_duplicate=False,
                        created_at=placed_at,
                    )
                )
                # Inject a genuine double charge on a small fraction of orders.
                if rng.random() < 0.04:
                    pay_seq += 1
                    payments.append(
                        Payment(
                            id=f"PAY-{pay_seq:06d}",
                            customer_id=cid,
                            order_id=oid,
                            amount=total,
                            status="succeeded",
                            method="card",
                            is_duplicate=True,
                            created_at=placed_at + timedelta(minutes=rng.randint(1, 30)),
                        )
                    )

        # ~40% have a subscription.
        if rng.random() < 0.4:
            sub_seq += 1
            started = now - timedelta(days=rng.randint(30, 720))
            sub_status = rng.choices(["active", "paused", "cancelled"], weights=[75, 10, 15])[0]
            monthly = rng.choice([19.0, 29.0, 39.0])
            subscriptions.append(
                Subscription(
                    id=f"SUB-{sub_seq:06d}",
                    customer_id=cid,
                    plan=rng.choice(["monthly-box", "wellness-plus", "linen-club"]),
                    status=sub_status,
                    monthly_amount=monthly,
                    started_at=started,
                    next_renewal=(now + timedelta(days=rng.randint(1, 30)))
                    if sub_status == "active"
                    else None,
                    cancelled_effective=(now + timedelta(days=rng.randint(1, 30)))
                    if sub_status == "cancelled"
                    else None,
                )
            )

    tickets = _demo_tickets(customers, orders, payments, subscriptions)

    # Compute summary stats before objects are detached by the session commit.
    n_double = sum(1 for p in payments if p.is_duplicate)

    with session_scope() as s:
        for batch in (customers, orders, subscriptions, payments, tickets):
            for row in batch:
                s.add(row)

    # KB into Chroma.
    n_kb = kb.index_articles(KB_ARTICLES)

    return {
        "customers": len(customers),
        "orders": len(orders),
        "subscriptions": len(subscriptions),
        "payments": len(payments),
        "tickets": len(tickets),
        "kb_articles": n_kb,
        "double_charges": n_double,
    }


def reset_and_seed() -> dict[str, int]:
    import agent_ops.backend.models  # noqa: F401  (register tables)

    engine = get_engine()
    SQLModel.metadata.drop_all(engine)
    init_db()
    return generate()


def main() -> None:
    counts = reset_and_seed()
    print("Seeded Aurora mock backend:")
    for k, v in counts.items():
        print(f"  {k:>16}: {v}")


if __name__ == "__main__":
    main()
