"""Build and persist a replayable JSON trace for a run.

Test we hold it to: from a trace alone you can reconstruct exactly what the
agent did and why — the plan, every tool call with args + result, every
guardrail decision, token/cost/latency, and the final outcome.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from agent_ops.backend.db import session_scope
from agent_ops.backend.models import TraceRecord
from agent_ops.config import get_settings


def summarize(state: dict[str, Any]) -> dict[str, Any]:
    usage = state.get("usage", [])
    tool_calls = [e for e in state.get("trace_events", []) if e.get("type") == "tool_call"]
    total_cost = round(sum(u.get("cost_usd", 0.0) for u in usage), 6)
    total_tokens = sum(u.get("tokens_in", 0) + u.get("tokens_out", 0) for u in usage)
    total_latency = round(sum(u.get("latency_ms", 0.0) for u in usage), 1)
    return {
        "intent": state.get("intent"),
        "status": (state.get("resolution") or {}).get("status"),
        "stop_reason": state.get("stop_reason"),
        "tool_calls": len(tool_calls),
        "iterations": state.get("iterations", 0),
        "llm_calls": len(usage),
        "total_tokens": total_tokens,
        "total_cost_usd": total_cost,
        "total_latency_ms": total_latency,
        "escalated": state.get("escalated", False),
    }


def write_trace(state: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Write the full JSON trace to disk and upsert a TraceRecord. Returns
    (path, summary)."""
    s = get_settings()
    s.traces_dir.mkdir(parents=True, exist_ok=True)
    run_id = state["run_id"]
    summary = summarize(state)
    trace = {
        "run_id": run_id,
        "ticket_id": state.get("ticket_id"),
        "customer_id": state.get("customer_id"),
        "intent": state.get("intent"),
        "request_text": state.get("request_text"),
        "status": summary["status"],
        "created_at": datetime.now(UTC).isoformat(),
        "plan": state.get("plan"),
        "events": state.get("trace_events", []),
        "usage": state.get("usage", []),
        "resolution": state.get("resolution"),
        "summary": summary,
    }
    path = s.traces_dir / f"{run_id}.json"
    path.write_text(json.dumps(trace, indent=2, default=str), encoding="utf-8")

    with session_scope() as sess:
        rec = sess.get(TraceRecord, run_id)
        if rec is None:
            rec = TraceRecord(run_id=run_id)
            sess.add(rec)
        rec.ticket_id = state.get("ticket_id")
        rec.customer_id = state.get("customer_id")
        rec.intent = state.get("intent")
        rec.status = summary["status"]
        rec.path = str(path)
        rec.summary = summary

    return str(path), summary
