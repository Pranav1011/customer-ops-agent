"""The policy engine — the single authority that gates every state-changing
action *before* it executes.

It is deliberately deterministic and free of any LLM: given a proposed tool call
and the run context, it returns allow / escalate / block with the rule that
fired. This is the most safety-critical code in the system and is hard-tested in
tests/test_policy.py — a bug here is the worst failure mode.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from agent_ops.backend.db import session_scope
from agent_ops.backend.models import Order
from agent_ops.config import get_settings

Effect = Literal["allow", "escalate", "block"]

# Tools that change state. Anything not here is a read and is never gated.
WRITE_TOOLS = {
    "issue_refund",
    "cancel_subscription",
    "update_shipping_address",
    "apply_account_credit",
    "create_followup_task",
    "send_customer_reply",
    "update_crm_record",
    "escalate_to_human",
}

# Statuses where a shipping address can no longer be changed.
_UNCHANGEABLE_ADDRESS_STATUSES = {"shipped", "delivered", "returned", "cancelled"}


@dataclass
class PolicyDecision:
    effect: Effect
    rule: str
    reason: str

    @property
    def allowed(self) -> bool:
        return self.effect == "allow"


def _allow(
    rule: str = "default_allow", reason: str = "no policy restriction applies"
) -> PolicyDecision:
    return PolicyDecision("allow", rule, reason)


def _escalate(rule: str, reason: str) -> PolicyDecision:
    return PolicyDecision("escalate", rule, reason)


def _block(rule: str, reason: str) -> PolicyDecision:
    return PolicyDecision("block", rule, reason)


def evaluate_action(
    tool: str,
    args: dict[str, Any],
    *,
    authorized_customer: str | None,
    identity_verified: bool | None = None,
) -> PolicyDecision:
    """Evaluate a proposed write action. `authorized_customer` is the ticket's
    own customer; actions may never target anyone else."""
    settings = get_settings()

    # Reads are never gated.
    if tool not in WRITE_TOOLS:
        return _allow("not_a_write_tool", "read tools are ungated")

    # escalate_to_human is always safe.
    if tool == "escalate_to_human":
        return _allow("escalation_always_allowed", "handing off to a human is always permitted")

    # --- Scope: no action outside the current ticket's customer. ---
    scope = _check_scope(tool, args, authorized_customer)
    if scope is not None:
        return scope

    # --- Per-tool rules. ---
    if tool == "issue_refund":
        return _refund_policy(args)
    if tool == "apply_account_credit":
        return _credit_policy(args, settings.credit_direct_limit)
    if tool == "cancel_subscription":
        return _escalate(
            "cancellation_requires_human",
            "subscription cancellations are irreversible and always require human confirmation",
        )
    if tool == "update_shipping_address":
        return _address_policy(args, identity_verified)
    if tool == "update_crm_record":
        return _crm_policy(args, identity_verified)

    # create_followup_task, send_customer_reply — low-risk writes.
    return _allow(f"{tool}_low_risk", "low-risk write permitted")


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _order(order_id: str) -> dict[str, Any] | None:
    with session_scope() as s:
        o = s.get(Order, order_id)
        if o is None:
            return None
        return {
            "customer_id": o.customer_id,
            "total": o.total,
            "refunded_amount": o.refunded_amount,
            "status": o.status,
        }


def _check_scope(
    tool: str, args: dict[str, Any], authorized_customer: str | None
) -> PolicyDecision | None:
    """Block any action whose target customer differs from the ticket's customer."""
    # Direct customer_id argument.
    target = args.get("customer_id")
    if target and authorized_customer and target != authorized_customer:
        return _block(
            "cross_customer", f"action targets {target} but ticket belongs to {authorized_customer}"
        )

    # order_id argument -> resolve to its owner.
    order_id = args.get("order_id")
    if order_id:
        o = _order(order_id)
        if o is None:
            return _block("order_not_found", f"order {order_id} does not exist")
        if authorized_customer and o["customer_id"] != authorized_customer:
            return _block(
                "cross_customer",
                f"order {order_id} belongs to {o['customer_id']}, not {authorized_customer}",
            )
    return None


def _refund_policy(args: dict[str, Any]) -> PolicyDecision:
    settings = get_settings()
    amount = float(args.get("amount", 0) or 0)
    order_id = args.get("order_id")

    if amount <= 0:
        return _block("refund_nonpositive", "refund amount must be positive")

    o = _order(order_id) if order_id else None
    if o is not None:
        remaining = round(o["total"] - o["refunded_amount"], 2)
        if amount > remaining + 1e-6:
            return _block(
                "refund_exceeds_order",
                f"refund {amount} exceeds refundable remaining {remaining} on order {order_id}",
            )

    if amount > settings.refund_approval_threshold:
        return _escalate(
            "refund_over_threshold",
            f"refund {amount} exceeds the ${settings.refund_approval_threshold:.0f} auto-approval threshold",
        )
    return _allow("refund_within_policy", f"refund {amount} within policy")


def _credit_policy(args: dict[str, Any], direct_limit: float) -> PolicyDecision:
    amount = float(args.get("amount", 0) or 0)
    if amount <= 0:
        return _block("credit_nonpositive", "credit amount must be positive")
    if amount > direct_limit:
        return _escalate(
            "credit_over_limit",
            f"goodwill credit {amount} exceeds the ${direct_limit:.0f} direct-apply limit",
        )
    return _allow("credit_within_limit", f"credit {amount} within direct-apply limit")


def _address_policy(args: dict[str, Any], identity_verified: bool | None) -> PolicyDecision:
    if not identity_verified:
        return _escalate("identity_unverified", "address changes require verified identity")
    order_id = args.get("order_id")
    o = _order(order_id) if order_id else None
    if o is not None and o["status"] in _UNCHANGEABLE_ADDRESS_STATUSES:
        return _block(
            "address_locked",
            f"order {order_id} is '{o['status']}'; address can no longer be changed",
        )
    return _allow("address_change_ok", "identity verified and order is still changeable")


def _crm_policy(args: dict[str, Any], identity_verified: bool | None) -> PolicyDecision:
    if not identity_verified:
        return _escalate("identity_unverified", "account changes require verified identity")
    return _allow("crm_update_ok", "identity verified")
