"""Write tools — state-changing actions.

Every write is schema-validated (via the registry), logged to `ActionLog` with
enough information to reverse it in the mock, and — critically — gated by the
policy engine *before* it runs (enforced centrally in the agent's act node).
The tools also re-check their own hard invariants as defense in depth.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field

from agent_ops.backend.db import session_scope
from agent_ops.backend.models import (
    ActionLog,
    Customer,
    Escalation,
    Order,
    Payment,
    Subscription,
)
from agent_ops.tools.registry import ToolContext, ToolResult, register

_CRM_ALLOWED_FIELDS = {"prefers_channel", "notes", "tier"}


def _log(
    ctx: ToolContext,
    tool: str,
    args: dict[str, Any],
    result: dict[str, Any],
    *,
    ok: bool,
    reversible: bool,
) -> int:
    with session_scope() as s:
        entry = ActionLog(
            run_id=ctx.run_id,
            ticket_id=ctx.ticket_id,
            customer_id=ctx.customer_id,
            tool=tool,
            args=args,
            result=result,
            ok=ok,
            reversible=reversible,
        )
        s.add(entry)
        s.flush()
        return entry.id


# --------------------------------------------------------------------------- #
# issue_refund
# --------------------------------------------------------------------------- #
class IssueRefundArgs(BaseModel):
    order_id: str = Field(description="Order to refund against, e.g. ORD-000123")
    amount: float = Field(gt=0, description="Refund amount in the order currency")
    reason: str = Field(description="Why the refund is being issued")


@register(
    name="issue_refund",
    description="Refund an amount against an order to the original payment method. Amount must not exceed the refundable remaining on the order.",
    kind="write",
    args_model=IssueRefundArgs,
)
def issue_refund(ctx: ToolContext, args: IssueRefundArgs) -> ToolResult:
    with session_scope() as s:
        o = s.get(Order, args.order_id)
        if o is None:
            res = {"error": f"not_found: order {args.order_id}"}
            _log(ctx, "issue_refund", args.model_dump(), res, ok=False, reversible=False)
            return ToolResult(ok=False, error=res["error"])
        remaining = round(o.total - o.refunded_amount, 2)
        if args.amount > remaining + 1e-6:
            res = {"error": f"conflict: refund {args.amount} exceeds refundable {remaining}"}
            _log(ctx, "issue_refund", args.model_dump(), res, ok=False, reversible=False)
            return ToolResult(ok=False, error=res["error"])

        prior = o.refunded_amount
        o.refunded_amount = round(prior + args.amount, 2)
        refund_id = f"RFN-{uuid.uuid4().hex[:8].upper()}"
        s.add(
            Payment(
                id=refund_id,
                customer_id=o.customer_id,
                order_id=o.id,
                amount=args.amount,
                status="refunded",
                method="card",
                created_at=datetime.now(UTC),
            )
        )
        data = {
            "refund_id": refund_id,
            "order_id": o.id,
            "amount": args.amount,
            "reason": args.reason,
            "order_refunded_total": o.refunded_amount,
            "_undo": {
                "order_id": o.id,
                "restore_refunded_amount": prior,
                "refund_payment_id": refund_id,
            },
        }
    _log(ctx, "issue_refund", args.model_dump(), data, ok=True, reversible=True)
    return ToolResult(ok=True, data=data)


# --------------------------------------------------------------------------- #
# cancel_subscription
# --------------------------------------------------------------------------- #
class CancelSubscriptionArgs(BaseModel):
    customer_id: str = Field(description="Customer whose subscription to cancel")
    effective_date: str | None = Field(
        default=None, description="ISO date; defaults to end of current cycle"
    )


@register(
    name="cancel_subscription",
    description="Cancel a customer's subscription effective at the end of the current cycle. Irreversible account change — policy requires human approval first.",
    kind="write",
    args_model=CancelSubscriptionArgs,
    reversible=False,
)
def cancel_subscription(ctx: ToolContext, args: CancelSubscriptionArgs) -> ToolResult:
    with session_scope() as s:
        sub = s.query(Subscription).filter(Subscription.customer_id == args.customer_id).first()
        if sub is None:
            res = {"error": f"not_found: subscription for {args.customer_id}"}
            _log(ctx, "cancel_subscription", args.model_dump(), res, ok=False, reversible=False)
            return ToolResult(ok=False, error=res["error"])
        prior_status = sub.status
        sub.status = "cancelled"
        sub.cancelled_effective = datetime.now(UTC)
        data = {
            "subscription_id": sub.id,
            "customer_id": args.customer_id,
            "status": "cancelled",
            "_undo": {"subscription_id": sub.id, "restore_status": prior_status},
        }
    _log(ctx, "cancel_subscription", args.model_dump(), data, ok=True, reversible=True)
    return ToolResult(ok=True, data=data)


# --------------------------------------------------------------------------- #
# update_shipping_address
# --------------------------------------------------------------------------- #
class Address(BaseModel):
    line1: str
    city: str
    region: str
    postal_code: str
    country: str = "US"


class UpdateAddressArgs(BaseModel):
    order_id: str
    address: Address


@register(
    name="update_shipping_address",
    description="Change the shipping address on an order. Only permitted before the order ships, and only with verified identity.",
    kind="write",
    args_model=UpdateAddressArgs,
)
def update_shipping_address(ctx: ToolContext, args: UpdateAddressArgs) -> ToolResult:
    with session_scope() as s:
        o = s.get(Order, args.order_id)
        if o is None:
            res = {"error": f"not_found: order {args.order_id}"}
            _log(ctx, "update_shipping_address", args.model_dump(), res, ok=False, reversible=False)
            return ToolResult(ok=False, error=res["error"])
        if o.status in {"shipped", "delivered", "returned", "cancelled"}:
            res = {"error": f"conflict: order is '{o.status}'; address cannot be changed"}
            _log(ctx, "update_shipping_address", args.model_dump(), res, ok=False, reversible=False)
            return ToolResult(ok=False, error=res["error"])
        prior = dict(o.shipping_address)
        o.shipping_address = args.address.model_dump()
        data = {
            "order_id": o.id,
            "shipping_address": o.shipping_address,
            "_undo": {"order_id": o.id, "restore_address": prior},
        }
    _log(ctx, "update_shipping_address", args.model_dump(), data, ok=True, reversible=True)
    return ToolResult(ok=True, data=data)


# --------------------------------------------------------------------------- #
# apply_account_credit
# --------------------------------------------------------------------------- #
class ApplyCreditArgs(BaseModel):
    customer_id: str
    amount: float = Field(gt=0)
    reason: str


@register(
    name="apply_account_credit",
    description="Apply goodwill account credit to a customer. Credits above the direct-apply limit require approval.",
    kind="write",
    args_model=ApplyCreditArgs,
)
def apply_account_credit(ctx: ToolContext, args: ApplyCreditArgs) -> ToolResult:
    credit_id = f"CRD-{uuid.uuid4().hex[:8].upper()}"
    data = {
        "credit_id": credit_id,
        "customer_id": args.customer_id,
        "amount": args.amount,
        "reason": args.reason,
    }
    _log(ctx, "apply_account_credit", args.model_dump(), data, ok=True, reversible=True)
    return ToolResult(ok=True, data=data)


# --------------------------------------------------------------------------- #
# create_followup_task
# --------------------------------------------------------------------------- #
class FollowupArgs(BaseModel):
    customer_id: str
    description: str
    due: str | None = Field(default=None, description="ISO date the task is due")


@register(
    name="create_followup_task",
    description="Create a follow-up task when a resolution depends on a future event (a reship arriving, a refund posting, a carrier investigation).",
    kind="write",
    args_model=FollowupArgs,
)
def create_followup_task(ctx: ToolContext, args: FollowupArgs) -> ToolResult:
    task_id = f"TSK-{uuid.uuid4().hex[:8].upper()}"
    data = {"task_id": task_id, **args.model_dump()}
    _log(ctx, "create_followup_task", args.model_dump(), data, ok=True, reversible=True)
    return ToolResult(ok=True, data=data)


# --------------------------------------------------------------------------- #
# send_customer_reply
# --------------------------------------------------------------------------- #
class SendReplyArgs(BaseModel):
    ticket_id: str
    message: str


@register(
    name="send_customer_reply",
    description="Send a reply to the customer on a ticket.",
    kind="write",
    args_model=SendReplyArgs,
)
def send_customer_reply(ctx: ToolContext, args: SendReplyArgs) -> ToolResult:
    data = {"ticket_id": args.ticket_id, "delivered": True, "chars": len(args.message)}
    _log(ctx, "send_customer_reply", {"ticket_id": args.ticket_id}, data, ok=True, reversible=False)
    return ToolResult(ok=True, data=data)


# --------------------------------------------------------------------------- #
# update_crm_record
# --------------------------------------------------------------------------- #
class UpdateCrmArgs(BaseModel):
    customer_id: str
    fields: dict[str, Any] = Field(description="Fields to update (prefers_channel, notes, tier)")


@register(
    name="update_crm_record",
    description="Update safe CRM fields on a customer (prefers_channel, notes, tier). Requires verified identity.",
    kind="write",
    args_model=UpdateCrmArgs,
)
def update_crm_record(ctx: ToolContext, args: UpdateCrmArgs) -> ToolResult:
    bad = set(args.fields) - _CRM_ALLOWED_FIELDS
    if bad:
        return ToolResult(ok=False, error=f"invalid_fields: {sorted(bad)}")
    with session_scope() as s:
        c = s.get(Customer, args.customer_id)
        if c is None:
            return ToolResult(ok=False, error=f"not_found: customer {args.customer_id}")
        prior = {k: getattr(c, k) for k in args.fields}
        for k, v in args.fields.items():
            setattr(c, k, v)
        data = {
            "customer_id": c.id,
            "updated": args.fields,
            "_undo": {"customer_id": c.id, "restore": prior},
        }
    _log(ctx, "update_crm_record", args.model_dump(), data, ok=True, reversible=True)
    return ToolResult(ok=True, data=data)


# --------------------------------------------------------------------------- #
# escalate_to_human
# --------------------------------------------------------------------------- #
class EscalateArgs(BaseModel):
    ticket_id: str | None = None
    reason: str
    context: dict[str, Any] = Field(default_factory=dict)


@register(
    name="escalate_to_human",
    description="Route the ticket to a human specialist with a reason and context. Always permitted; a correct escalation is a successful outcome.",
    kind="write",
    args_model=EscalateArgs,
    reversible=False,
)
def escalate_to_human(ctx: ToolContext, args: EscalateArgs) -> ToolResult:
    with session_scope() as s:
        esc = Escalation(
            ticket_id=args.ticket_id or ctx.ticket_id,
            customer_id=ctx.customer_id,
            run_id=ctx.run_id,
            reason=args.reason,
            context=args.context,
            status="open",
        )
        s.add(esc)
        s.flush()
        data = {"escalation_id": esc.id, "reason": args.reason}
    _log(ctx, "escalate_to_human", {"reason": args.reason}, data, ok=True, reversible=False)
    return ToolResult(ok=True, data=data)


# --------------------------------------------------------------------------- #
# reversibility (mock): undo a logged action
# --------------------------------------------------------------------------- #
def undo_action(action_id: int) -> ToolResult:
    """Reverse a previously logged, reversible action. Demonstrates that every
    write in the mock can be rolled back."""
    with session_scope() as s:
        entry = s.get(ActionLog, action_id)
        if entry is None:
            return ToolResult(ok=False, error=f"not_found: action {action_id}")
        if not entry.reversible or entry.reversed:
            return ToolResult(ok=False, error="not_reversible_or_already_reversed")
        undo = (entry.result or {}).get("_undo")
        if not undo:
            return ToolResult(ok=False, error="no_undo_information")

        if entry.tool == "issue_refund":
            o = s.get(Order, undo["order_id"])
            if o is not None:
                o.refunded_amount = undo["restore_refunded_amount"]
            pay = s.get(Payment, undo["refund_payment_id"])
            if pay is not None:
                s.delete(pay)
        elif entry.tool == "update_shipping_address":
            o = s.get(Order, undo["order_id"])
            if o is not None:
                o.shipping_address = undo["restore_address"]
        elif entry.tool == "cancel_subscription":
            sub = s.get(Subscription, undo["subscription_id"])
            if sub is not None:
                sub.status = undo["restore_status"]
                sub.cancelled_effective = None
        elif entry.tool == "update_crm_record":
            c = s.get(Customer, undo["customer_id"])
            if c is not None:
                for k, v in undo["restore"].items():
                    setattr(c, k, v)
        else:
            return ToolResult(ok=False, error=f"undo_not_supported: {entry.tool}")

        entry.reversed = True
        return ToolResult(ok=True, data={"reversed_action": action_id, "tool": entry.tool})
