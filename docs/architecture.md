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

---

## System design

### Request path (async job queue)

Agent runs are slow and variable (a local LLM is ~30–60s per ticket), so resolution runs
off the request path on a bounded worker pool:

```
POST /jobs ──▶ create Job(queued) ──▶ ThreadPoolExecutor (size = WORKER_CONCURRENCY)
   │                                        │
   └── 202 {job_id} (immediate)             ├─ Job(running) → agent loop → Job(succeeded, run_id)
                                            └─ on error   → Job(failed, error)
GET /jobs/{id}  ◀── client polls for terminal status, then GET /trace/{run_id}
```

The pool size **bounds concurrency** (backpressure): excess submissions queue rather than
overwhelming the model. The API stays responsive; the UI submits, shows a "reasoning…"
state, and polls. `GET /metrics` exposes throughput, escalation rate, cost/latency, queue
depth, and action counts for monitoring.

### Swappable brain behind one interface

`LLMProvider` (`classify / plan / decide / compose_reply / score_reply / compare`) has
three implementations — `mock` (rules), `ollama` (local LLM), `anthropic` (Claude) —
selected by one env var. Nothing else in the system changes. This is what makes the
model-comparison harness and the "safety decoupled from model quality" result possible.

### Reliability boundaries (defense in depth)

1. **Input** — untrusted ticket text is sanitized for prompt-injection before classification.
2. **Every write** — gated by the deterministic policy engine (thresholds, identity,
   scope, cancellation) *before* execution, at a single choke point (and re-checked inside
   each tool).
3. **The loop** — max-iteration cap, per-task cost ceiling, and a repeated-call loop-breaker.
4. **Output** — reply grounding check (no un-sourced order ids reach the customer).
5. **Fallback** — anything uncertain escalates to a human; escalation is a success outcome.

Because these are deterministic and independent of the model, a weak model degrades to safe
escalation rather than unsafe action (measured: llama3.1:8b = 25% task success, **100%
action safety**).

### Data & state

SQLite (WAL + busy-timeout for concurrent workers) via SQLModel; Chroma for the KB with
local embeddings; per-run JSON traces on disk referenced from a `TraceRecord`. The data
layer is isolated behind `backend/db.py`, so Postgres is a driver swap, not a rewrite.

### Deploy

`docker compose up --build` → containerized API (uv-installed, seeds on first boot, volume
for data). Mock brain by default (offline); point at host Ollama or Claude via env.

### Production evolution (what changes at scale)

| Concern | Today (laptop) | Production |
|---|---|---|
| Queue | in-process `ThreadPoolExecutor` + DB rows | Redis/SQS + dedicated worker deployment |
| DB | SQLite (WAL) | Postgres (connection pool, read replicas) |
| Tracing | JSON files + `TraceRecord` | OpenTelemetry → a trace backend (Langfuse/LangSmith) |
| Vector store | Chroma (local) | managed pgvector / Qdrant |
| Metrics | `/metrics` JSON | Prometheus scrape + Grafana; alerts on action-safety drop |
| Scaling | one process | stateless API + horizontal workers behind a queue |
| Evals | `make eval` + CI gate | same harness in CI, blocking deploys on safety floor |

The interfaces (queue, provider, data layer, tracing) are already the shapes these
production components expect, so each is a swap rather than a redesign.
