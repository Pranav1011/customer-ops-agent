"""Long-term customer memory across tickets.

Two of the three standard scopes are modeled explicitly (procedural memory is a
noted stretch):
  * episodic — this customer's past interactions/resolutions.
  * semantic — durable facts/preferences (tier, VIP, preferred channel, learned
    signals like how often they request refunds).

Read on intake (context injection) and written on resolve. Also exposed to the
agent as a read tool it may choose to call mid-run.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from agent_ops.backend.db import session_scope
from agent_ops.backend.models import Customer, MemoryProfile

_EPISODIC_CAP = 20
_REFUND_INTENTS = {"refund", "double_charge", "damaged_item"}


def _now() -> datetime:
    return datetime.now(UTC)


def load_profile(customer_id: str) -> dict[str, Any]:
    with session_scope() as s:
        m = s.get(MemoryProfile, customer_id)
        if m is None:
            return {"customer_id": customer_id, "semantic": {}, "episodic": []}
        return {
            "customer_id": customer_id,
            "semantic": dict(m.semantic),
            "episodic": list(m.episodic),
        }


def recall(customer_id: str) -> dict[str, Any]:
    """Compact recall injected into context at intake."""
    prof = load_profile(customer_id)
    episodic = prof["episodic"]
    return {
        "semantic": prof["semantic"],
        "recent_resolutions": episodic[-3:],
        "interaction_count": len(episodic),
        "returning_customer": len(episodic) > 0,
    }


def record_resolution(customer_id: str | None, entry: dict[str, Any]) -> None:
    """Append an episodic memory and refresh the semantic profile."""
    if not customer_id:
        return
    with session_scope() as s:
        m = s.get(MemoryProfile, customer_id)
        cust = s.get(Customer, customer_id)
        if m is None:
            m = MemoryProfile(customer_id=customer_id, semantic={}, episodic=[])
            s.add(m)

        episodic = list(m.episodic)
        episodic.append(entry)
        episodic = episodic[-_EPISODIC_CAP:]
        m.episodic = episodic

        semantic = dict(m.semantic)
        if cust is not None:
            semantic["tier"] = cust.tier
            semantic["vip"] = cust.tier == "vip"
            semantic["prefers_channel"] = cust.prefers_channel
            semantic["tenure_days"] = cust.tenure_days
        semantic["interactions"] = len(episodic)
        semantic["refund_requests"] = sum(1 for e in episodic if e.get("intent") in _REFUND_INTENTS)
        m.semantic = semantic
        m.updated_at = _now()
