"""LangGraph node functions: intake -> plan -> act (loop) -> resolve."""

from __future__ import annotations

import re
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any

from langchain_core.runnables import RunnableConfig

from agent_ops.agent.schemas import (
    Decision,
    DecisionAction,
    Intent,
    IntentResult,
    Plan,
    Resolution,
)
from agent_ops.agent.state import AgentState
from agent_ops.backend.db import session_scope
from agent_ops.backend.models import Escalation, Ticket
from agent_ops.config import get_settings
from agent_ops.llm.base import LLMProvider
from agent_ops.tools.registry import REGISTRY, ToolContext

_ORDER_RE = re.compile(r"ORD-\d{4,6}", re.IGNORECASE)


def _provider(config: RunnableConfig) -> LLMProvider:
    return config["configurable"]["provider"]


def _ctx(state: AgentState) -> ToolContext:
    return ToolContext(
        run_id=state["run_id"],
        ticket_id=state.get("ticket_id"),
        customer_id=state.get("customer_id"),
    )


def _event(state: AgentState, etype: str, **payload: Any) -> None:
    events = state.setdefault("trace_events", [])
    events.append(
        {
            "seq": len(events),
            "type": etype,
            "ts": datetime.now(UTC).isoformat(),
            **payload,
        }
    )


def _drain(provider: LLMProvider, state: AgentState) -> None:
    usage = state.setdefault("usage", [])
    for ev in provider.drain_usage():
        usage.append(asdict(ev))


def _total_cost(state: AgentState) -> float:
    return sum(u.get("cost_usd", 0.0) for u in state.get("usage", []))


# --------------------------------------------------------------------------- #
# intake
# --------------------------------------------------------------------------- #
def intake(state: AgentState, config: RunnableConfig) -> AgentState:
    provider = _provider(config)
    text = state["request_text"]

    result: IntentResult = provider.classify(text)
    _drain(provider, state)
    state["intent"] = result.intent.value
    state["intent_confidence"] = result.confidence

    if not state.get("order_id"):
        m = _ORDER_RE.search(text)
        if m:
            state["order_id"] = m.group(0).upper()

    # Pull the customer profile so downstream steps know identity/tier.
    if state.get("customer_id"):
        res = REGISTRY.run("get_customer", {"customer_id": state["customer_id"]}, _ctx(state))
        state.setdefault("scratchpad", []).append(
            {
                "tool": "get_customer",
                "args": {"customer_id": state["customer_id"]},
                "result": res.to_dict(),
            }
        )
        if res.ok:
            state["identity_verified"] = res.data.get("identity_verified")
        _event(
            state,
            "tool_call",
            tool="get_customer",
            args={"customer_id": state["customer_id"]},
            result=res.to_dict(),
        )

    _event(
        state,
        "intent",
        intent=state["intent"],
        confidence=result.confidence,
        order_id=state.get("order_id"),
        rationale=result.rationale,
    )
    return state


# --------------------------------------------------------------------------- #
# plan
# --------------------------------------------------------------------------- #
def plan(state: AgentState, config: RunnableConfig) -> AgentState:
    provider = _provider(config)
    intent = IntentResult(
        intent=Intent(state["intent"]), confidence=state.get("intent_confidence", 0.5)
    )
    context = {
        "customer_id": state.get("customer_id"),
        "order_id": state.get("order_id"),
        "identity_verified": state.get("identity_verified"),
    }
    p: Plan = provider.plan(intent, state["request_text"], context)
    _drain(provider, state)
    state["plan"] = p.model_dump()
    _event(state, "plan", plan=state["plan"])
    return state


# --------------------------------------------------------------------------- #
# act (one decide+execute step per invocation; loops via conditional edge)
# --------------------------------------------------------------------------- #
def _view(state: AgentState) -> dict[str, Any]:
    return {
        "intent": state.get("intent"),
        "request_text": state.get("request_text"),
        "customer_id": state.get("customer_id"),
        "order_id": state.get("order_id"),
        "identity_verified": state.get("identity_verified"),
        "plan": state.get("plan"),
        "scratchpad": state.get("scratchpad", []),
        "iterations": state.get("iterations", 0),
        "escalated": state.get("escalated", False),
    }


def act(state: AgentState, config: RunnableConfig) -> AgentState:
    provider = _provider(config)
    settings = get_settings()

    decision: Decision = provider.decide(_view(state))
    _drain(provider, state)
    state["iterations"] = state.get("iterations", 0) + 1
    _event(
        state,
        "decision",
        action=decision.action.value,
        tool=decision.tool,
        args=decision.args,
        rationale=decision.rationale,
        confidence=decision.confidence,
    )

    if decision.action == DecisionAction.finish:
        state["done"] = True
        state["stop_reason"] = "finish"
        return state

    if decision.action == DecisionAction.escalate:
        state["escalated"] = True
        state["escalation_reason"] = decision.args.get("reason") or decision.rationale
        state["done"] = True
        state["stop_reason"] = "escalate"
        _event(state, "escalation", reason=state["escalation_reason"])
        return state

    # call_tool
    tool = decision.tool or ""
    spec = REGISTRY.get(tool)
    if spec is None:
        # Model hallucinated a tool — record and let the loop continue/finish.
        res = {"ok": False, "data": {}, "error": f"unknown_tool: {tool}"}
        state.setdefault("scratchpad", []).append(
            {"tool": tool, "args": decision.args, "result": res}
        )
        _event(state, "tool_call", tool=tool, args=decision.args, result=res)
    else:
        result = REGISTRY.run(tool, decision.args, _ctx(state))
        state.setdefault("scratchpad", []).append(
            {"tool": tool, "args": decision.args, "result": result.to_dict()}
        )
        _event(state, "tool_call", tool=tool, args=decision.args, result=result.to_dict())

    # Budget guardrails.
    if state["iterations"] >= settings.max_iterations and not state.get("done"):
        state["done"] = True
        state["stop_reason"] = "max_iterations"
        _event(state, "guard", decision="stop", reason="max_iterations")
    elif _total_cost(state) >= settings.cost_ceiling_usd and not state.get("done"):
        state["done"] = True
        state["stop_reason"] = "cost_ceiling"
        _event(state, "guard", decision="stop", reason="cost_ceiling")

    return state


def act_router(state: AgentState) -> str:
    return "resolve" if state.get("done") else "act"


# --------------------------------------------------------------------------- #
# resolve
# --------------------------------------------------------------------------- #
def resolve(state: AgentState, config: RunnableConfig) -> AgentState:
    provider = _provider(config)

    escalated = state.get("escalated", False)
    forced = state.get("stop_reason") in ("max_iterations", "cost_ceiling")
    if forced and not escalated:
        # Ran out of budget without a clean finish -> fail safe by escalating.
        escalated = True
        state["escalated"] = True
        state["escalation_reason"] = (
            f"stopped on {state['stop_reason']} without a confident resolution"
        )
        _event(state, "escalation", reason=state["escalation_reason"])

    view = _view(state)
    view["escalated"] = escalated
    reply = provider.compose_reply(view)
    _drain(provider, state)

    status = "escalated" if escalated else "resolved"
    resolution = Resolution(
        status=status,
        customer_reply=reply,
        actions_taken=state.get("actions_taken", []),
        escalation_reason=state.get("escalation_reason"),
        confidence=state.get("intent_confidence", 1.0),
    )
    state["resolution"] = resolution.model_dump()
    _event(state, "reply", status=status, customer_reply=reply)

    _persist(state, status, reply)
    return state


def _persist(state: AgentState, status: str, reply: str) -> None:
    with session_scope() as s:
        if state.get("ticket_id"):
            t = s.get(Ticket, state["ticket_id"])
            if t is not None:
                t.status = "escalated" if status == "escalated" else "resolved"
                t.intent = state.get("intent")
                t.resolution_status = status
                t.run_id = state["run_id"]
        if state.get("escalated"):
            s.add(
                Escalation(
                    ticket_id=state.get("ticket_id"),
                    customer_id=state.get("customer_id"),
                    run_id=state["run_id"],
                    reason=state.get("escalation_reason") or "escalated",
                    context={"intent": state.get("intent"), "request": state.get("request_text")},
                    status="open",
                )
            )
