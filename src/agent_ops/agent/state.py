"""The agent's run state (LangGraph channel schema).

Everything here is JSON-serializable so the SqliteSaver checkpointer can persist
thread state — that persistence is our short-term memory across turns of a
single ticket.
"""

from __future__ import annotations

from typing import Any, TypedDict


class AgentState(TypedDict, total=False):
    # Identity of the run / work item.
    run_id: str
    ticket_id: str | None
    customer_id: str | None
    order_id: str | None  # extracted hint from the request
    request_text: str

    # Reasoning outputs.
    intent: str
    intent_confidence: float
    plan: dict[str, Any]

    # Tool loop working memory.
    scratchpad: list[dict[str, Any]]  # [{tool, args, result}]
    iterations: int
    identity_verified: bool | None

    # Control / outcome.
    done: bool
    escalated: bool
    escalation_reason: str | None
    stop_reason: str | None  # finish | escalate | max_iterations | cost_ceiling
    actions_taken: list[str]
    resolution: dict[str, Any]

    # Observability (serializable, folded into the trace at resolve).
    trace_events: list[dict[str, Any]]
    usage: list[dict[str, Any]]


def new_state(
    *,
    run_id: str,
    request_text: str,
    ticket_id: str | None = None,
    customer_id: str | None = None,
    order_id: str | None = None,
) -> AgentState:
    return AgentState(
        run_id=run_id,
        ticket_id=ticket_id,
        customer_id=customer_id,
        order_id=order_id,
        request_text=request_text,
        intent="",
        intent_confidence=0.0,
        plan={},
        scratchpad=[],
        iterations=0,
        identity_verified=None,
        done=False,
        escalated=False,
        escalation_reason=None,
        stop_reason=None,
        actions_taken=[],
        resolution={},
        trace_events=[],
        usage=[],
    )
