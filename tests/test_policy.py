"""Hard, deterministic tests for the policy engine.

The policy engine is the most safety-critical component: a bug here means the
agent takes an action it never should have. These tests call it directly (no
LLM) so they are fast and exact.
"""

from __future__ import annotations

from sqlmodel import select

import agent_ops.tools  # noqa: F401  (register tools)
from agent_ops.backend.db import session_scope
from agent_ops.backend.models import Customer, Order
from agent_ops.policy.engine import evaluate_action

VERIFIED = True
UNVERIFIED = False


def _order_where(status: str, verified: bool | None = None) -> dict:
    with session_scope() as s:
        for o in s.exec(select(Order).where(Order.status == status)).all():
            c = s.get(Customer, o.customer_id)
            if verified is None or c.identity_verified == verified:
                return {
                    "id": o.id,
                    "customer_id": o.customer_id,
                    "total": o.total,
                    "refunded": o.refunded_amount,
                    "status": o.status,
                }
    raise AssertionError(f"no order with status={status} verified={verified}")


# --- refunds ---
def test_refund_within_threshold_allowed():
    o = _order_where("delivered")
    d = evaluate_action(
        "issue_refund",
        {"order_id": o["id"], "amount": 10.0, "reason": "x"},
        authorized_customer=o["customer_id"],
    )
    assert d.effect == "allow", d


def test_refund_over_threshold_but_within_order_escalates():
    # Find an order whose total is above the approval threshold.
    with session_scope() as s:
        o = s.exec(select(Order).where(Order.total > 120)).first()
        oid, cid = o.id, o.customer_id
    d = evaluate_action(
        "issue_refund", {"order_id": oid, "amount": 110.0, "reason": "x"}, authorized_customer=cid
    )
    assert d.effect == "escalate" and d.rule == "refund_over_threshold"


def test_refund_nonpositive_blocked():
    o = _order_where("delivered")
    d = evaluate_action(
        "issue_refund",
        {"order_id": o["id"], "amount": 0, "reason": "x"},
        authorized_customer=o["customer_id"],
    )
    assert d.effect == "block" and d.rule == "refund_nonpositive"


def test_refund_exceeding_order_total_blocked():
    o = _order_where("delivered")
    d = evaluate_action(
        "issue_refund",
        {"order_id": o["id"], "amount": o["total"] + 50, "reason": "x"},
        authorized_customer=o["customer_id"],
    )
    assert d.effect == "block" and d.rule == "refund_exceeds_order"


# --- cross-customer scope ---
def test_cross_customer_refund_blocked():
    o = _order_where("delivered")
    d = evaluate_action(
        "issue_refund",
        {"order_id": o["id"], "amount": 5.0, "reason": "x"},
        authorized_customer="CUST-99999",
    )
    assert d.effect == "block" and d.rule == "cross_customer"


def test_cross_customer_credit_blocked():
    d = evaluate_action(
        "apply_account_credit",
        {"customer_id": "CUST-00001", "amount": 5, "reason": "x"},
        authorized_customer="CUST-00002",
    )
    assert d.effect == "block" and d.rule == "cross_customer"


# --- cancellation ---
def test_cancellation_always_escalates():
    d = evaluate_action(
        "cancel_subscription", {"customer_id": "CUST-00001"}, authorized_customer="CUST-00001"
    )
    assert d.effect == "escalate" and d.rule == "cancellation_requires_human"


# --- address changes / identity ---
def test_address_change_requires_identity():
    o = _order_where("placed", verified=UNVERIFIED)
    d = evaluate_action(
        "update_shipping_address",
        {
            "order_id": o["id"],
            "address": {"line1": "x", "city": "y", "region": "z", "postal_code": "1"},
        },
        authorized_customer=o["customer_id"],
        identity_verified=UNVERIFIED,
    )
    assert d.effect == "escalate" and d.rule == "identity_unverified"


def test_address_change_blocked_after_shipment():
    o = _order_where("shipped", verified=VERIFIED)
    d = evaluate_action(
        "update_shipping_address",
        {
            "order_id": o["id"],
            "address": {"line1": "x", "city": "y", "region": "z", "postal_code": "1"},
        },
        authorized_customer=o["customer_id"],
        identity_verified=VERIFIED,
    )
    assert d.effect == "block" and d.rule == "address_locked"


def test_address_change_allowed_when_verified_and_pre_ship():
    o = _order_where("placed", verified=VERIFIED)
    d = evaluate_action(
        "update_shipping_address",
        {
            "order_id": o["id"],
            "address": {"line1": "x", "city": "y", "region": "z", "postal_code": "1"},
        },
        authorized_customer=o["customer_id"],
        identity_verified=VERIFIED,
    )
    assert d.effect == "allow"


# --- credit ---
def test_credit_within_limit_allowed():
    d = evaluate_action(
        "apply_account_credit",
        {"customer_id": "CUST-00001", "amount": 10, "reason": "x"},
        authorized_customer="CUST-00001",
    )
    assert d.effect == "allow"


def test_credit_over_limit_escalates():
    d = evaluate_action(
        "apply_account_credit",
        {"customer_id": "CUST-00001", "amount": 100, "reason": "x"},
        authorized_customer="CUST-00001",
    )
    assert d.effect == "escalate" and d.rule == "credit_over_limit"


# --- always-safe / ungated ---
def test_escalation_always_allowed():
    d = evaluate_action("escalate_to_human", {"reason": "x"}, authorized_customer="CUST-00001")
    assert d.effect == "allow"


def test_read_tool_not_gated():
    d = evaluate_action("get_order", {"order_id": "ORD-000001"}, authorized_customer="CUST-00001")
    assert d.effect == "allow" and d.rule == "not_a_write_tool"
