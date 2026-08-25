# DECISIONS.md

A running trail of meaningful engineering choices — what was chosen, what was
rejected, and why. This is a portfolio asset: it's the "shows judgment" signal.

---

### D1 — LLM: deterministic mock now, real Claude behind a one-line swap
**Chose:** A `MockChatModel(BaseChatModel)` as the default LLM provider, selected
via `LLM_PROVIDER=mock`. It emits structured plans and tool-calls deterministically
from rule-based per-intent handlers. A `ChatAnthropic` provider (`LLM_PROVIDER=anthropic`)
is wired but inactive until an API key is added.
**Rejected:** Requiring a live API key from day one.
**Why:** No key was available at build time, and — more importantly — the interview
signal of this project is the *machinery around the model* (real actions, guardrails,
memory, tracing, evals), all of which is genuinely exercised by a deterministic mock.
A deterministic model also makes the whole system and the eval harness reproducible in
CI with zero cost. Swapping to real reasoning is a single env var; the same eval harness
then measures real model quality and drives a model-comparison table.

### D2 — Framework: LangGraph
**Chose:** LangGraph `StateGraph` with a `SqliteSaver` checkpointer.
**Rejected:** A hand-rolled ReAct loop on the raw Anthropic SDK.
**Why:** LangGraph gives first-class stateful orchestration and a checkpointer that
doubles as short-term (thread) memory, and it's the exact keyword most agentic JDs list.
The hand-rolled loop is more "look, no abstraction" but reimplements state/checkpointing
we'd get for free. Tracing is kept custom (see D5) so we still own the observability story.

### D3 — Data store: SQLite via SQLModel
**Chose:** SQLite (single file) modelled with SQLModel (SQLAlchemy + Pydantic).
**Rejected:** Postgres from the start.
**Why:** Zero-setup, laptop-friendly, and the domain fits comfortably in a file DB at
the seed sizes we need. SQLModel keeps the ORM types and the API schemas unified. The
data layer is isolated so a Postgres migration later is a driver change, not a rewrite.

### D4 — KB embeddings: Chroma's built-in ONNX MiniLM (offline)
**Chose:** Chroma with its default `all-MiniLM-L6-v2` ONNX embedding function.
**Rejected:** `sentence-transformers` (pulls in torch — heavy, and shaky wheels on 3.13)
and cloud embedding APIs (need a key).
**Why:** Keeps retrieval real and fully offline with a small footprint. The model is
downloaded once on first seed, then runs locally with no network and no key.

### D5 — Tracing: custom structured JSON traces, not LangSmith
**Chose:** Emit a full replayable JSON trace per run (plan, every tool call + args +
result, guardrail decisions, simulated tokens/cost/latency, final outcome), persisted to
disk and referenced from the DB.
**Rejected:** LangSmith.
**Why:** No vendor lock, no key, and building it ourselves *is* the observability signal.
The test we hold it to: from a trace alone you can reconstruct exactly what the agent did
and why.

### D6 — Long-term memory: plain tables, not a memory framework
**Chose:** Roll our own episodic (past resolutions) and semantic (durable prefs/facts)
stores as tables, read on intake and written on resolve.
**Rejected:** Mem0 / LangMem / Zep.
**Why:** At this scope a framework is more surface area than value. The taxonomy
(short-term / episodic / semantic / procedural) is demonstrated explicitly in our own
code, which reads better in an interview than delegating it to a black box.

### D8 — Guardrails enforced at a single choke point in the agent loop
**Chose:** A deterministic, LLM-free policy engine (`policy/engine.py`) called from the
agent's `act` node *before* any write tool executes; it returns allow / escalate / block
with the rule that fired. The write tools also re-check their own hard invariants as
defense in depth. Thresholds: refunds > $100 escalate; goodwill credit > $25 escalates;
subscription cancellations always escalate; address/CRM changes require verified identity;
address changes blocked once shipped; no action outside the ticket's customer; refund ≤
refundable remaining. Confidence < 0.6 on a write escalates.
**Rejected:** Trusting the model to self-police, or gating inside each tool only.
**Why:** A single, testable choke point can't be bypassed by a clever prompt, is trivial
to unit-test exhaustively (tests/test_policy.py), and keeps the safety logic auditable in
one place. Escalation is modeled as a *successful* outcome, not a failure.

### D9 — Eval scenarios are self-contained; the judge is validated; injection is sanitized
**Chose:** (a) Each golden scenario declares its own backend fixtures, so the harness
resets to a known state per scenario and expected final state is exactly checkable —
rather than depending on random-seed picks. (b) The LLM-as-judge is measured, not trusted:
a pairwise position-consistency study, repetition-stability, and human-agreement on a
hand-labeled subset (per Shi et al. 2406.07791). (c) A prompt-injection scanner sanitizes
untrusted ticket text before classification (strips embedded "SYSTEM:"/"ignore previous"
instructions), so injected commands can't steer the agent.
**Rejected:** Depending on the shared seed for expected outcomes; a bare judge with no
reliability check; relying on the model alone to resist injection.
**Why:** Determinism makes the harness reproducible and CI-gateable; a validated judge is
far more credible; and input sanitization is a concrete, testable OWASP-LLM defense. The
first full run caught a real classifier mis-route (logged in ERROR_ANALYSIS.md) — evidence
the harness earns its keep.

### D13 — System-design layer: async job queue, observability, containerization
**Chose:** Move ticket resolution off the request path onto a bounded
`ThreadPoolExecutor` worker pool (`POST /jobs` → 202 + `job_id`; poll `GET /jobs/{id}`),
enabled SQLite WAL + busy-timeout for concurrent writes, added a `GET /metrics` aggregate
endpoint, and a `Dockerfile` + `docker-compose.yml` for one-command deploy. Documented the
production-evolution path (Redis/SQS queue, Postgres, OTel tracing, Prometheus) in the
architecture doc.
**Rejected:** Blocking the HTTP request for the full agent run (bad UX with a ~40s local
LLM); pulling in Redis/Celery/Postgres now (over-engineering for a laptop — the spec's
anti-goal).
**Why:** Long-running agent work needs async processing + backpressure; a bounded in-process
pool demonstrates the pattern with zero extra infra, and the interfaces (queue, data layer,
tracing) are deliberately the same shapes their production equivalents expect, so scaling is
a swap not a rewrite. This adds the system-design/architecture signal without violating
"runs on a laptop."

### D12 — MCP server + a real model-comparison table
**Chose:** Expose the tool registry as an MCP server (official `mcp` 2.x `MCPServer`,
stdio), generating each tool's input schema from its Pydantic args model and routing write
tools through the same policy engine — so guardrails hold even when tools are driven from
Claude Desktop/Cursor. Added `make compare` (`eval/compare.py`) that runs one slice under
`mock` and `ollama` and writes a quality/safety/cost/latency table.
**Why:** The MCP surface matches where the industry is going and reuses the exact tool +
policy layer (no duplication). The comparison produced the project's sharpest result:
llama3.1:8b scored **25% task success but 100% action safety** vs mock's 100/100 — concrete
evidence that safety is decoupled from model quality. Note: `mcp` on PyPI resolved to a
newer 2.x with a restructured API (no `@server.list_tools()`); used `MCPServer.add_tool`
with dynamically-built typed wrappers to get flat, described schemas.

### D11 — Real LLM reasoning via local Ollama (free), not just Claude
**Chose:** A third provider, `OllamaProvider` (`LLM_PROVIDER=ollama`), that drives the
agent's reasoning with a local model (default `llama3.1:8b`) through the same
`PromptedProvider` base as the Claude provider. Refactored the shared prompt/JSON/retry
logic into `llm/prompted.py` so Claude and Ollama don't duplicate it, and added
repair-retry + safe fallbacks (fail toward escalation) for when a small local model emits
malformed JSON.
**Rejected:** Requiring a paid Claude key to run as a "real" agent; wiring Ollama's
native tool-calling (varies by model — prompt-based JSON is uniform and robust).
**Why:** It makes the project a genuine LLM agent — the model actually classifies,
plans, chooses tools, and writes replies — for **$0, fully local, no key**. Verified live:
a WISMO ticket resolves grounded in real order data, and a $172 refund is still escalated
by the deterministic policy engine (guardrails hold regardless of the model). The mock
brain stays the default for tests/CI/evals because it's instant and reproducible; local
8B inference is ~30–60s per ticket.

### D10 — Frontend: thin React + Vite + TypeScript console
**Chose:** A small React/TS SPA (Vite dev server on :5173) talking to the FastAPI JSON
API over CORS — a queue, a run-trace viewer, and an escalation inbox, on a single
warm-dark color family (60/30/10). Added `POST /tickets/{id}/resolve` so seeded tickets
are runnable from the UI.
**Rejected:** A zero-build server-rendered console (simpler, but weaker "JS/React" signal)
and any heavier app framework (overkill for a demo surface).
**Why:** JDs list JS/React, and the trace viewer is the single most persuasive demo of the
agent's reasoning + guardrails. Kept intentionally thin — the UI is the demo surface, not
the substance — and designed to not read as a generic AI dashboard.

### D7 — Domain skin: "Aurora", a DTC e-commerce + subscription brand
**Chose:** A thin e-commerce/subscription skin over a domain-agnostic core (~90% of the
code is domain-neutral; e-commerce assumptions live only in the tool/data/seed layer).
**Why:** The owner has an e-commerce analytics background, so realistic messy seed data
is cheap and makes retrieval + memory actually matter. The same agent could be re-pointed
at IT triage or claims by swapping tools + data only.
