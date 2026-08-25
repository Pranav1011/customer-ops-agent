"""Integration tests: guardrails firing through the full agent loop, plus
action logging and reversibility."""

from __future__ import annotations

from sqlmodel import select

import agent_ops.tools  # noqa: F401
from agent_ops.agent.graph import run_ticket
from agent_ops.backend.db import session_scope
from agent_ops.backend.models import ActionLog, Escalation, Order, Payment
from agent_ops.tools.registry import ToolContext
from agent_ops.tools.write_tools import IssueRefundArgs, issue_refund, undo_action


def _fresh_order(max_total: float | None = None, min_total: float | None = None) -> dict:
    with session_scope() as s:
        stmt = select(Order).where(Order.status == "delivered", Order.refunded_amount == 0.0)
        for o in s.exec(stmt).all():
            if max_total is not None and o.total > max_total:
                continue
            if min_total is not None and o.total < min_total:
                continue
            return {"id": o.id, "customer_id": o.customer_id, "total": o.total}
    raise AssertionError("no matching fresh order")


def test_small_refund_auto_resolved_and_logged():
    o = _fresh_order(max_total=100.0)
    r = run_ticket(
        f"Please refund order {o['id']}, it wasn't what I expected.",
        ticket_id="TCK-RF-SMALL",
        customer_id=o["customer_id"],
    )
    assert r["status"] == "resolved" and not r["escalated"]
    with session_scope() as s:
        logs = s.exec(
            select(ActionLog).where(
                ActionLog.run_id == r["run_id"], ActionLog.tool == "issue_refund"
            )
        ).all()
        assert any(x.ok for x in logs)
        order = s.get(Order, o["id"])
        assert order.refunded_amount > 0


def test_large_refund_escalates_and_takes_no_action():
    o = _fresh_order(min_total=120.0)
    r = run_ticket(
        f"Refund order {o['id']} in full please.",
        ticket_id="TCK-RF-LARGE",
        customer_id=o["customer_id"],
    )
    assert r["status"] == "escalated" and r["escalated"]
    with session_scope() as s:
        # No successful refund should have been executed for this run.
        logs = s.exec(
            select(ActionLog).where(
                ActionLog.run_id == r["run_id"],
                ActionLog.tool == "issue_refund",
            )
        ).all()
        assert not any(x.ok for x in logs)
        order = s.get(Order, o["id"])
        assert order.refunded_amount == 0


def test_cancellation_escalates():
    r = run_ticket(
        "Please cancel my subscription now.", ticket_id="TCK-CX", customer_id="CUST-00005"
    )
    assert r["escalated"]
    with session_scope() as s:
        assert (
            s.exec(select(Escalation).where(Escalation.ticket_id == "TCK-CX")).first() is not None
        )


def test_refund_is_reversible():
    o = _fresh_order(max_total=100.0)
    ctx = ToolContext(run_id="rev-test", ticket_id="TCK-REV", customer_id=o["customer_id"])
    res = issue_refund(ctx, IssueRefundArgs(order_id=o["id"], amount=5.0, reason="test"))
    assert res.ok
    with session_scope() as s:
        log = s.exec(
            select(ActionLog).where(
                ActionLog.run_id == "rev-test", ActionLog.tool == "issue_refund"
            )
        ).first()
        action_id = log.id
        refund_payment_id = res.data["refund_id"]
        assert s.get(Order, o["id"]).refunded_amount == 5.0

    undo = undo_action(action_id)
    assert undo.ok
    with session_scope() as s:
        assert s.get(Order, o["id"]).refunded_amount == 0.0
        assert s.get(Payment, refund_payment_id) is None


# --- loop-breaker + grounding guards (Phase 5.1) ---
from agent_ops.agent.graph import build_graph  # noqa: E402
from agent_ops.agent.schemas import (  # noqa: E402
    Decision,
    DecisionAction,
    Intent,
    IntentResult,
    Plan,
)
from agent_ops.llm.base import JudgeResult, LLMProvider  # noqa: E402


class _StubProvider(LLMProvider):
    """Minimal provider; subclasses override decide/compose_reply."""

    def classify(self, request_text):
        return IntentResult(intent=Intent.order_status, confidence=0.9)

    def plan(self, intent, request_text, context):
        return Plan(intent=Intent.order_status, summary="stub", steps=[])

    def decide(self, view):
        return Decision(action=DecisionAction.finish)

    def compose_reply(self, view):
        return "ok"

    def score_reply(self, rubric, reply):
        return JudgeResult(score=1.0, verdict="pass")

    def compare(self, rubric, a, b):
        return "A", JudgeResult(score=1.0, verdict="A")


def _run_with(provider: LLMProvider, **state_kw):
    from agent_ops.agent.state import new_state

    g = build_graph()
    st = new_state(
        run_id=state_kw.pop("run_id", "stub"), request_text="where is my order", **state_kw
    )
    cfg = {"configurable": {"provider": provider, "thread_id": st["run_id"]}, "recursion_limit": 40}
    return g.invoke(st, config=cfg)


def test_loop_breaker_escalates_on_repeated_tool_call():
    class Loopy(_StubProvider):
        def decide(self, view):
            # Always call the same tool with the same args -> a runaway loop.
            return Decision(
                action=DecisionAction.call_tool,
                tool="get_order",
                args={"order_id": "ORD-000001"},
                confidence=1.0,
            )

    final = _run_with(Loopy(), run_id="loop", customer_id="CUST-00001")
    assert final["escalated"] is True
    assert any(e.get("decision") == "loop_break" for e in final["trace_events"])
    # It must not have spun all the way to the iteration cap.
    assert final["iterations"] <= 3


def test_grounding_guard_escalates_on_hallucinated_order_id():
    class Halluc(_StubProvider):
        def decide(self, view):
            return Decision(action=DecisionAction.finish)

        def compose_reply(self, view):
            return "Good news — your order ORD-999999 was delivered yesterday!"

    final = _run_with(Halluc(), run_id="halluc", customer_id="CUST-00001")
    assert final["escalated"] is True
    assert any(e.get("decision") == "grounding_fail" for e in final["trace_events"])
    # The hallucinated id must not reach the customer reply.
    assert "ORD-999999" not in final["resolution"]["customer_reply"]
