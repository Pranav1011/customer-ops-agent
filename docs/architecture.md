# Architecture

Aurora is a **single-agent** customer-operations worker. A work item (ticket) flows
through an intake → plan → act → resolve loop, taking real write actions against a mock
backend, with a policy layer gating every state change and a tracing layer recording
everything.

```
                 ┌─────────────────────────────────────────────┐
   ticket ─────▶ │                 AGENT CORE                   │
 (POST /tickets) │  intake → plan → act (tool loop) → resolve   │
                 │     │           │            │       │       │
                 └─────┼───────────┼────────────┼───────┼───────┘
                       │           │            │       │
                ┌──────▼───┐  ┌────▼─────┐  ┌───▼─────┐ │
                │  MEMORY  │  │  TOOLS / │  │GUARDRAILS│ │
                │ short +  │  │  ACTIONS │  │ + policy │ │
                │  long    │  │read/write│  │+ escalate│ │
                └──────────┘  └────┬─────┘  └──────────┘ │
                                   │                      │
                        ┌──────────▼──────────┐    ┌──────▼───────┐
                        │    MOCK BACKEND     │    │   TRACING     │
                        │ orders / customers/ │    │ replayable    │
                        │ subs / payments /   │    │ JSON per run  │
                        │ tickets / KB        │    └───────────────┘
                        └─────────────────────┘

  Cross-cutting:  EVAL HARNESS (golden set · metrics · LLM-judge)  ·  FastAPI + (thin UI)
```

## The loop (LangGraph `StateGraph`)

1. **intake** — classify intent (cheap model role), load customer profile + history +
   long-term memory, attach the (untrusted) ticket text.
2. **plan** — emit a structured `Plan` (ordered steps, the tool each needs, expected
   outcome, risk level). This is the most interview-legible artifact; the prompt lives in
   a versioned file.
3. **act** — ReAct-style tool loop. After each tool result the agent decides the next
   step. A `SqliteSaver` checkpointer holds thread state (short-term memory). Hard
   max-iteration cap + per-task cost ceiling + runaway-loop breaker.
4. **guard** — the policy engine runs *before* any state-changing tool executes; it can
   allow, block, or force escalation. Low confidence also routes to escalation.
5. **resolve** — produce the customer-facing reply, update records, write long-term
   memory, and emit the full trace.

## Layers

- **`llm/`** — provider abstraction. `MockChatModel` (deterministic, offline) and
  `ChatAnthropic` (real) behind a role router (`classifier` / `reasoner` / `judge`).
- **`backend/`** — SQLite (SQLModel) mock systems + Chroma KB + deterministic seed
  generator.
- **`tools/`** — typed, docstringed read/write tools in a registry; write tools are
  logged, reversible in the mock, and policy-gated. Realistic error injection.
- **`policy/`** — explicit, unit-tested rules (refund thresholds, always-escalate
  cancellations, identity verification, no cross-customer actions, refund ≤ order total).
- **`memory/`** — short-term (checkpointer + compaction) and long-term (episodic +
  semantic) stores.
- **`tracing/`** — structured replayable traces.
- **`eval/`** — golden dataset, deterministic state checks, validated LLM-as-judge,
  metrics (task success / trajectory / action safety / efficiency), error analysis.
- **`api/`** — FastAPI service (submit ticket, get resolution, queue, trace, escalations).

## Portability

The agent core is domain-agnostic. Re-pointing Aurora at IT triage, insurance claims, or
HR onboarding means swapping `tools/`, `backend/models.py` + `seed.py`, and the KB — the
loop, policy engine, memory, tracing, and eval harness are unchanged.
