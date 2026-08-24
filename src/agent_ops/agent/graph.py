"""Assemble and run the agent as a LangGraph StateGraph.

    intake -> plan -> act ──(not done)──> act
                         └──(done)──────> resolve -> END

A SqliteSaver checkpointer persists thread state keyed by thread_id — this is
the agent's short-term memory across turns of a single ticket.
"""

from __future__ import annotations

import sqlite3
import uuid
from functools import lru_cache
from typing import Any

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph

import agent_ops.tools  # noqa: F401  (registers read/write tools on import)
from agent_ops.agent import nodes
from agent_ops.agent.state import AgentState, new_state
from agent_ops.config import get_settings
from agent_ops.llm.router import get_provider
from agent_ops.tracing.trace import write_trace


@lru_cache
def _checkpointer() -> SqliteSaver:
    s = get_settings()
    s.db_file.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(s.db_file.parent / "checkpoints.db"), check_same_thread=False)
    return SqliteSaver(conn)


@lru_cache
def build_graph():
    g = StateGraph(AgentState)
    g.add_node("intake", nodes.intake)
    g.add_node("plan", nodes.plan)
    g.add_node("act", nodes.act)
    g.add_node("resolve", nodes.resolve)

    g.add_edge(START, "intake")
    g.add_edge("intake", "plan")
    g.add_edge("plan", "act")
    g.add_conditional_edges("act", nodes.act_router, {"act": "act", "resolve": "resolve"})
    g.add_edge("resolve", END)

    return g.compile(checkpointer=_checkpointer())


def run_ticket(
    request_text: str,
    *,
    ticket_id: str | None = None,
    customer_id: str | None = None,
    order_id: str | None = None,
    thread_id: str | None = None,
) -> dict[str, Any]:
    """Resolve one work item end to end and persist its trace."""
    settings = get_settings()
    run_id = uuid.uuid4().hex[:12]
    provider = get_provider()
    graph = build_graph()

    initial = new_state(
        run_id=run_id,
        request_text=request_text,
        ticket_id=ticket_id,
        customer_id=customer_id,
        order_id=order_id,
    )
    config = {
        "configurable": {"provider": provider, "thread_id": thread_id or run_id},
        "recursion_limit": settings.max_iterations + 12,
    }
    final: AgentState = graph.invoke(initial, config=config)

    path, summary = write_trace(final)
    resolution = final.get("resolution", {})
    return {
        "run_id": run_id,
        "ticket_id": ticket_id,
        "customer_id": customer_id,
        "intent": final.get("intent"),
        "status": resolution.get("status"),
        "escalated": final.get("escalated", False),
        "customer_reply": resolution.get("customer_reply", ""),
        "escalation_reason": final.get("escalation_reason"),
        "trace_path": path,
        "summary": summary,
    }
