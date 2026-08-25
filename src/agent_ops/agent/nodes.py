"""LangGraph node functions: intake -> plan -> act (loop) -> resolve."""

from __future__ import annotations

import json
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
from agent_ops.memory.long_term import recall, record_resolution
from agent_ops.memory.short_term import compact_scratchpad
from agent_ops.policy.engine import evaluate_action
from agent_ops.policy.injection import scan
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


def _do_escalate(state: AgentState, reason: str, rule: str | None = None) -> None:
    state["escalated"] = True
    state["escalation_reason"] = reason
    state["done"] = True
    state["stop_reason"] = "escalate"
    _event(state, "escalation", reason=reason, rule=rule)


# --------------------------------------------------------------------------- #
# intake
# --------------------------------------------------------------------------- #
def intake(state: AgentState, config: RunnableConfig) -> AgentState:
    provider = _provider(config)
    raw = state["request_text"]

    # Prompt-injection defense: sanitize untrusted text before classification.
    text, injected, markers = scan(raw)
    state["injection_detected"] = injected
    if injected:
        _event(
            state,
            "guard",
            decision="sanitize_input",
            reason="prompt_injection_markers",
            markers=markers,
        )

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

        # Long-term memory recall (episodic + semantic) injected as context.
        mem = recall(state["customer_id"])
        state["memory"] = mem
        _event(state, "memory_recall", memory=mem)

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
        "memory": state.get("memory", {}),
        "plan": state.get("plan"),
        "scratchpad": state.get("scratchpad", []),
        "iterations": state.get("iterations", 0),
        "escalated": state.get("escalated", False),
    }


def act(state: AgentState, config: RunnableConfig) -> AgentState:
    provider = _provider(config)
    settings = get_settings()

    # Short-term memory compaction: keep the context handed to the model small
    # on long threads without losing which tools ran and whether they succeeded.
    compacted, n = compact_scratchpad(state.get("scratchpad", []))
    if n:
        state["scratchpad"] = compacted
        state["compactions"] = state.get("compactions", 0) + 1
        _event(state, "compaction", entries_compacted=n)

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
        _do_escalate(state, decision.args.get("reason") or decision.rationale)
        return state

    # call_tool
    tool = decision.tool or ""

    # escalate_to_human is a control action, not a normal tool execution.
    if tool == "escalate_to_human":
        _do_escalate(state, decision.args.get("reason") or decision.rationale)
        return state

    # Guardrail: runaway-loop breaker. A real (esp. small) model can get stuck
    # re-calling the same tool without making progress. If it repeats an
    # identical call, or calls the same tool a 3rd time, stop and escalate.
    sig = tool + ":" + json.dumps(decision.args, sort_keys=True, default=str)
    sigs = state.setdefault("call_sigs", [])
    same_tool = sum(1 for s in sigs if s.split(":", 1)[0] == tool)
    if sig in sigs or same_tool >= 2:
        _event(
            state,
            "guard",
            decision="loop_break",
            tool=tool,
            reason="repeated tool call without progress",
        )
        _do_escalate(
            state,
            f"runaway loop detected — repeated '{tool}' call without progress",
            rule="loop_breaker",
        )
        return state
    sigs.append(sig)

    spec = REGISTRY.get(tool)
    if spec is None:
        # Model hallucinated a tool — record and let the loop continue/finish.
        res = {"ok": False, "data": {}, "error": f"unknown_tool: {tool}"}
        state.setdefault("scratchpad", []).append(
            {"tool": tool, "args": decision.args, "result": res}
        )
        _event(state, "tool_call", tool=tool, args=decision.args, result=res)
    elif spec.kind == "write":
        # Guardrail 1: confidence gating — a low-confidence write escalates.
        if decision.confidence < settings.confidence_threshold:
            _do_escalate(
                state,
                f"low confidence ({decision.confidence:.2f}) on write '{tool}'",
                rule="confidence_gate",
            )
            return state

        # Guardrail 2: the policy engine gates every write BEFORE it executes.
        policy = evaluate_action(
            tool,
            decision.args,
            authorized_customer=state.get("customer_id"),
            identity_verified=state.get("identity_verified"),
        )
        _event(
            state, "guard", tool=tool, effect=policy.effect, rule=policy.rule, reason=policy.reason
        )

        if policy.effect == "escalate":
            _do_escalate(state, policy.reason, rule=policy.rule)
            return state
        if policy.effect == "block":
            res = {
                "ok": False,
                "data": {},
                "error": f"policy_blocked[{policy.rule}]: {policy.reason}",
            }
            state.setdefault("scratchpad", []).append(
                {"tool": tool, "args": decision.args, "result": res}
            )
            _event(state, "tool_call", tool=tool, args=decision.args, result=res, blocked=True)
        else:
            result = REGISTRY.run(tool, decision.args, _ctx(state))
            state.setdefault("scratchpad", []).append(
                {"tool": tool, "args": decision.args, "result": result.to_dict()}
            )
            if result.ok:
                state.setdefault("actions_taken", []).append(tool)
            _event(state, "tool_call", tool=tool, args=decision.args, result=result.to_dict())
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

    # Guardrail: grounding check. The reply must not cite an order id that never
    # appeared in a tool result (a hallucinated fact). If it does, discard the
    # reply and escalate rather than send an ungrounded claim to the customer.
    if not escalated:
        grounded = {
            m.upper()
            for m in re.findall(
                r"ORD-\d{3,6}", json.dumps(state.get("scratchpad", []), default=str), re.I
            )
        }
        if state.get("order_id"):
            grounded.add(state["order_id"].upper())
        cited = {m.upper() for m in re.findall(r"ORD-\d{3,6}", reply, re.I)}
        ungrounded = sorted(cited - grounded)
        if ungrounded:
            _event(
                state,
                "guard",
                decision="grounding_fail",
                reason=f"reply cited ungrounded order id(s): {ungrounded}",
            )
            escalated = True
            state["escalated"] = True
            state["escalation_reason"] = (
                f"reply referenced ungrounded order id(s) {ungrounded}; escalated to avoid a hallucinated claim"
            )
            reply = (
                "Thanks for reaching out — I want to make sure the details I share are exactly right, "
                "so I've routed this to a specialist who will follow up with you shortly."
            )
            _event(state, "escalation", reason=state["escalation_reason"], rule="grounding_guard")

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

    # Write an episodic memory for this customer (updates the semantic profile).
    if state.get("customer_id"):
        entry = {
            "run_id": state["run_id"],
            "ticket_id": state.get("ticket_id"),
            "intent": state.get("intent"),
            "status": status,
            "ts": datetime.now(UTC).isoformat(),
            "actions": state.get("actions_taken", []),
            "summary": reply[:160],
        }
        record_resolution(state["customer_id"], entry)
        _event(state, "memory_write", entry=entry)

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
