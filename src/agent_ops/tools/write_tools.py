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
from sqlalchemy import func, update
from sqlmodel import select

from agent_ops.backend.db import session_scope
from agent_ops.backend.escalations import open_escalation
from agent_ops.backend.ledger import refundable_remaining
from agent_ops.backend.models import (
    ActionLog,
    Customer,
    IdempotencyKey,
    Order,
    Payment,
    Subscription,
)
from agent_ops.backend.transitions import SUBSCRIPTION_TRANSITIONS, transition_error
from agent_ops.tools.registry import ToolContext, ToolResult, register

_CRM_ALLOWED_FIELDS = {"prefers_channel", "notes", "tier"}
# Tools undo_action knows how to reverse.
_UNDOABLE = ("issue_refund", "update_shipping_address", "update_crm_record")


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
    refund_id = f"RFN-{uuid.uuid4().hex[:8].upper()}"
    with session_scope() as s:
        # The cap check and the increment are one conditional UPDATE, issued
        # before any read so SQLite takes the write lock on fresh data: two
        # concurrent refunds cannot both pass the cap.
        paid = (
            select(func.coalesce(func.sum(Payment.amount), 0.0))
            .where(Payment.order_id == args.order_id, Payment.status == "succeeded")
            .scalar_subquery()
        )
        where = [Order.id == args.order_id, Order.refunded_amount + args.amount <= paid + 1e-6]
        if ctx.customer_id is not None:
            where.append(Order.customer_id == ctx.customer_id)
        moved = s.execute(
            update(Order)
            .where(*where)
            .values(refunded_amount=func.round(Order.refunded_amount + args.amount, 2))
            .execution_options(synchronize_session=False)
        ).rowcount
        o = s.get(Order, args.order_id)
        error = None
        if moved != 1:
            if o is None or (ctx.customer_id is not None and o.customer_id != ctx.customer_id):
                error = f"not_found: order {args.order_id}"
            else:
                paid_total, remaining = refundable_remaining(s, o)
                error = (
                    f"conflict: refund {args.amount} exceeds refundable {remaining} on order "
                    f"{o.id} (paid {paid_total}, already refunded {o.refunded_amount})"
                )
        else:
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
                    "restore_refunded_amount": round(o.refunded_amount - args.amount, 2),
                    "refund_payment_id": refund_id,
                },
            }
    # Logged after the order transaction ends: it holds the write lock.
    if error is not None:
        _log(ctx, "issue_refund", args.model_dump(), {"error": error}, ok=False, reversible=False)
        return ToolResult(ok=False, error=error)
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
        subs = s.exec(
            select(Subscription).where(Subscription.customer_id == args.customer_id)
        ).all()
        # Act on a live subscription when there is one; an old cancelled one only
        # answers when nothing else is left to cancel.
        sub = next((x for x in subs if x.status != "cancelled"), subs[0] if subs else None)
        if sub is None:
            res = {"error": f"not_found: subscription for {args.customer_id}"}
            _log(ctx, "cancel_subscription", args.model_dump(), res, ok=False, reversible=False)
            return ToolResult(ok=False, error=res["error"])
        err = transition_error(
            SUBSCRIPTION_TRANSITIONS, "subscription", sub.id, sub.status, "cancelled"
        )
        if err is not None:
            res = {"error": err}
            _log(ctx, "cancel_subscription", args.model_dump(), res, ok=False, reversible=False)
            return ToolResult(ok=False, error=err)
        prior_status = sub.status
        sub.status = "cancelled"
        sub.cancelled_effective = datetime.now(UTC)
        data = {
            "subscription_id": sub.id,
            "customer_id": args.customer_id,
            "status": "cancelled",
            "previous_status": prior_status,
        }
    _log(ctx, "cancel_subscription", args.model_dump(), data, ok=True, reversible=False)
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
        if o is None or (ctx.customer_id is not None and o.customer_id != ctx.customer_id):
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
        esc = open_escalation(
            s,
            ticket_id=args.ticket_id or ctx.ticket_id,
            customer_id=ctx.customer_id,
            run_id=ctx.run_id,
            reason=args.reason,
            context=args.context,
        )
        data = {"escalation_id": esc.id, "reason": args.reason}
    _log(ctx, "escalate_to_human", {"reason": args.reason}, data, ok=True, reversible=False)
    return ToolResult(ok=True, data=data)


# --------------------------------------------------------------------------- #
# reversibility (mock): undo a logged action
# --------------------------------------------------------------------------- #
def undo_action(action_id: int) -> ToolResult:
    """Reverse a previously logged, reversible action. Demonstrates that every
    reversible write in the mock can be rolled back.

    The action's idempotency key is deleted with it: the action no longer
    stands, so issuing it again on the same ticket must execute, not replay."""
    with session_scope() as s:
        # Claim the reversal first with a conditional UPDATE, so two concurrent
        # undos of one action cannot both apply it.
        claimed = s.execute(
            update(ActionLog)
            .where(
                ActionLog.id == action_id,
                ActionLog.reversed == False,  # noqa: E712
                ActionLog.reversible == True,  # noqa: E712
                ActionLog.tool.in_(_UNDOABLE),  # type: ignore[attr-defined]
            )
            .values(reversed=True)
            .execution_options(synchronize_session=False)
        ).rowcount
        entry = s.get(ActionLog, action_id)
        if entry is None:
            return ToolResult(ok=False, error=f"not_found: action {action_id}")
        undo = (entry.result or {}).get("_undo")
        if claimed != 1:
            if entry.reversed:
                return ToolResult(
                    ok=False, error=f"conflict: action {action_id} is already reversed"
                )
            if not entry.reversible:
                return ToolResult(
                    ok=False, error=f"conflict: action {action_id} ({entry.tool}) is irreversible"
                )
            if not undo:
                return ToolResult(ok=False, error="no_undo_information")
            return ToolResult(ok=False, error=f"undo_not_supported: {entry.tool}")
        if not undo:
            s.rollback()
            return ToolResult(ok=False, error="no_undo_information")

        if entry.tool == "issue_refund":
            o = s.get(Order, undo["order_id"])
            if o is not None:
                # Subtract this refund rather than restore the snapshot, so undoing
                # an earlier refund keeps any refund issued after it.
                amount = float((entry.args or {})["amount"])
                o.refunded_amount = round(max(o.refunded_amount - amount, 0.0), 2)
            pay = s.get(Payment, undo["refund_payment_id"])
            if pay is not None:
                s.delete(pay)
        elif entry.tool == "update_shipping_address":
            o = s.get(Order, undo["order_id"])
            if o is not None:
                o.shipping_address = undo["restore_address"]
        elif entry.tool == "update_crm_record":
            c = s.get(Customer, undo["customer_id"])
            if c is not None:
                for k, v in undo["restore"].items():
                    setattr(c, k, v)

        if entry.ticket_id is not None:
            # Lazy import: reliability.idempotency imports the tool registry.
            from agent_ops.reliability.idempotency import idempotency_key

            key = idempotency_key(entry.ticket_id, entry.tool, dict(entry.args or {}))
            stale = s.get(IdempotencyKey, key)
            if stale is not None:
                s.delete(stale)
        return ToolResult(ok=True, data={"reversed_action": action_id, "tool": entry.tool})
