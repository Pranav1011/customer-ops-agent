"""Short-term / working memory helpers.

The LangGraph SqliteSaver checkpointer already persists thread state across turns
of a ticket (that IS the short-term store). This module adds *compaction*: when
the tool-result scratchpad grows long, older verbose read-tool payloads are
summarized so the context handed to the model stays small, while the signals the
agent needs (which tools ran, and whether they succeeded) are preserved.
"""

from __future__ import annotations

import json
from typing import Any

# Read tools whose (potentially large) payloads are safe to summarize once old.
_COMPACTABLE_TOOLS = {
    "find_orders",
    "get_payment_history",
    "get_customer_history",
    "search_knowledge_base",
}


def _payload_size(entry: dict[str, Any]) -> int:
    return len(json.dumps(entry.get("result", {}), default=str))


def compact_scratchpad(
    scratchpad: list[dict[str, Any]], *, keep_last: int = 6, max_payload: int = 500
) -> tuple[list[dict[str, Any]], int]:
    """Return a compacted copy of the scratchpad plus the number of entries
    compacted. Entries within the most recent `keep_last` are never touched.
    Older entries from compactable read tools with large payloads have their
    data replaced by a short summary; the tool name and ok flag are preserved."""
    if len(scratchpad) <= keep_last:
        return list(scratchpad), 0

    cutoff = len(scratchpad) - keep_last
    out: list[dict[str, Any]] = []
    n_compacted = 0
    for i, entry in enumerate(scratchpad):
        if (
            i < cutoff
            and entry.get("tool") in _COMPACTABLE_TOOLS
            and _payload_size(entry) > max_payload
        ):
            result = entry.get("result", {})
            data = result.get("data", {}) or {}
            summary = f"{entry.get('tool')} -> ok={result.get('ok')}, keys={sorted(data)[:6]}"
            out.append(
                {
                    "tool": entry.get("tool"),
                    "args": entry.get("args", {}),
                    "result": {"ok": result.get("ok"), "data": {"_compacted": summary}},
                }
            )
            n_compacted += 1
        else:
            out.append(entry)
    return out, n_compacted
