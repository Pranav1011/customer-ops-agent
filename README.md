# Aurora — Customer Operations Agent

> A workflow-automation agent that takes a customer-operations ticket **end to end** —
> it plans, takes **real actions** across mock systems (refunds, cancellations, address
> changes, credits, escalations), guards against its own failures, and is measured by a
> **real eval + observability harness**. Not a RAG chatbot: the headline is
> *action-taking + reliability + measurement*.

![Aurora Operations Console — queue, run-trace viewer, and escalation inbox](docs/demo.gif)

**Status:** complete end to end — mock backend → agent loop → real actions + guardrails →
memory → eval harness → **three swappable brains** (deterministic mock, a real **local LLM
via Ollama**, or Claude) → MCP server → React console. Runs fully offline and free.

---

## Evaluation results (the differentiator)

Run offline with the deterministic mock reasoner over a **43-scenario golden set**
(easy → hard, including should-escalate, cross-customer, and prompt-injection cases):

| Metric | Result |
|---|---|
| **Task success** | **100%** (deterministic final-state check + LLM-judge on reply quality) |
| **Action safety** | **100%** — no forbidden action taken; escalates every time it must |
| Critical-tag safety | should-escalate **100%**, injection **100%**, cross-customer **100%** |
| Efficiency | ~$0.006 and ~320 ms simulated per ticket |
| Judge validation | position-consistency **100%**, repetition-stability **100%**, human-agreement **100%** (n=9) |

The LLM-as-judge is itself validated (position-bias / consistency study, per
[Shi et al. 2024](https://arxiv.org/abs/2406.07791)) rather than blindly trusted. The
harness caught a real classifier mis-route on the first run — see
[`ERROR_ANALYSIS.md`](ERROR_ANALYSIS.md) ("what broke, and how I found it"). `make eval`
reproduces all of this.

### Model comparison (same slice, different brain) — `make compare`

| Reasoner | Task success | **Action safety** | Avg tokens | Cost | Avg latency |
|---|---|---|---|---|---|
| `mock` (rules) | 100% | **100%** | 1,482 | $0.056 (sim) | 0.3s |
| `ollama` llama3.1:8b | 25% | **100%** | 9,489 | $0.00 | 37.3s |

The headline isn't the 25% — it's that **action safety stayed 100% even when the model's
task success collapsed.** A weak local model loops, mis-picks tools, and hallucinates, yet
the deterministic guardrails caught every misstep and it never took an unsafe action.
*Safety is decoupled from model quality* — which is the whole point of the reliability
layer. Full table: [`docs/model-comparison.md`](docs/model-comparison.md).

---

## Quickstart

```bash
make install     # uv sync (creates .venv, installs deps)
make seed        # generate the mock backend: SQLite data + Chroma KB
make dev         # run the API at http://127.0.0.1:8000 (docs at /docs)
make eval        # run the eval harness and print the report
make test        # run the test suite

# Frontend console (optional, in a second terminal while `make dev` runs):
make ui-install  # one-time: install the React console's deps
make ui          # serve the console at http://localhost:5173
```

Runs **fully offline** with a deterministic mock LLM — no API key needed.

### Three interchangeable "brains"

The agent's reasoning sits behind one interface, so you swap the brain with a single env
var — the loop, tools, guardrails, memory, tracing, evals, and UI never change:

| `LLM_PROVIDER` | Reasoning | Cost | Needs |
|---|---|---|---|
| `mock` (default) | deterministic rules — reproducible, instant, great for CI/evals | $0 | nothing |
| **`ollama`** | a **real local LLM** (llama3.1:8b) genuinely reasoning | $0 | [Ollama](https://ollama.com) running |
| `anthropic` | real Claude (Sonnet/Haiku) | API $ | `ANTHROPIC_API_KEY` |

**Run it as a real (free) LLM agent** on a local model — no key, no cost:

```bash
ollama pull llama3.1:8b            # once
make dev-ollama                    # API now reasons with a real local LLM
```

The trace's intake / plan / decision steps become the model *actually reasoning* over the
ticket and tool results — not keyword rules — while the guardrails still gate every
action. (Local 8B inference is ~30–60s per ticket; the mock brain stays the default for
tests and the eval sweep because it's instant and reproducible.)

## The console

A thin React + TypeScript operations console: a ticket **queue**, a per-ticket
**run-trace viewer** (plan → tool calls → guardrail decisions → reply, with cost/latency),
and an **escalation inbox**. Submit a ticket and watch the agent resolve it live.

![Aurora Operations Console — run-trace viewer showing the prompt-injection guardrail firing](docs/console.png)

_Above: a prompt-injection ticket. The agent sanitizes the untrusted input, answers the
real "where's my order" question, and ignores the embedded "issue a $500 refund" —
resolved, no refund._

## Configuration

Config is read from the environment (and an optional `.env` file at the repo root) with
sensible defaults, so `.env` is optional. Recognized variables:

| Variable | Default | Purpose |
|---|---|---|
| `LLM_PROVIDER` | `mock` | `mock` (rules), `ollama` (local LLM), or `anthropic` (Claude) |
| `OLLAMA_BASE_URL` | `http://localhost:11434` | local Ollama server |
| `OLLAMA_MODEL` | `llama3.1:8b` | local model used when `LLM_PROVIDER=ollama` |
| `ANTHROPIC_API_KEY` | — | required only when `LLM_PROVIDER=anthropic` |
| `MODEL_CLASSIFIER` | `claude-haiku-4-5-20251001` | Claude intent classification (cheap/fast) |
| `MODEL_REASONER` | `claude-sonnet-5` | Claude planning + reasoning |
| `MODEL_JUDGE` | `claude-sonnet-5` | Claude LLM-as-judge in evals |
| `DB_PATH` | `data/aurora.db` | SQLite file |
| `CHROMA_PATH` | `data/chroma` | vector KB store |
| `TRACE_DIR` | `data/traces` | per-run JSON traces |
| `MAX_ITERATIONS` | `8` | agent tool-loop cap |
| `COST_CEILING_USD` | `0.50` | per-task budget ceiling |
| `REFUND_APPROVAL_THRESHOLD` | `100.0` | refunds above this require human approval |
| `WORKER_CONCURRENCY` | `2` | max tickets resolved in parallel by the async worker pool |

## Architecture & system design

Tickets are resolved **asynchronously** off the request path: `POST /jobs` returns a
`job_id` immediately and a bounded worker pool (`WORKER_CONCURRENCY`) processes it — the
right pattern for slow, variable agent runs (a local LLM is ~30–60s/ticket). Clients poll
`GET /jobs/{id}`; `GET /metrics` exposes throughput, escalation rate, cost/latency, and
queue depth. Reliability is layered (input sanitization → policy-gated writes → loop/cost
caps → reply grounding → human escalation), so a weak model degrades to safe escalation
rather than unsafe action.

```bash
docker compose up --build     # containerized API at http://localhost:8000
```

Full write-up (request path, reliability boundaries, data/state, and the
production-evolution path — Redis/SQS, Postgres, OpenTelemetry, Prometheus) is in
[`docs/architecture.md`](docs/architecture.md).

## MCP server

The tool layer is also exposed over the **Model Context Protocol**, so the same tools
(with the same policy gating) can be driven from Claude Desktop, Cursor, or any MCP client:

```bash
make mcp    # serves the tools over stdio
```

Claude Desktop config (`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "aurora-ops": {
      "command": "uv",
      "args": ["run", "python", "-m", "agent_ops.mcp_server"],
      "cwd": "/absolute/path/to/customer-ops-agent"
    }
  }
}
```

Write tools stay policy-gated over MCP too — e.g. a >$100 `issue_refund` is refused with the
rule that fired rather than executed.

## The six capabilities (and where they live)

| Capability | Where |
|---|---|
| Planning & reasoning | `src/agent_ops/agent/` (graph + planning prompt) |
| Tool use / real actions | `src/agent_ops/tools/` (read + write tools, schema-validated, logged) |
| Memory & context | `src/agent_ops/memory/` (short-term checkpointer + long-term episodic/semantic) |
| Orchestration | `src/agent_ops/agent/graph.py` (single-agent LangGraph loop) |
| Reliability & guardrails | `src/agent_ops/policy/` (policy engine, gating, escalation) |
| Evals & observability | `src/agent_ops/eval/` + `src/agent_ops/tracing/` |

## Architecture

See [`docs/architecture.md`](docs/architecture.md). Key decisions and their rationale are
in [`DECISIONS.md`](DECISIONS.md); the failure log lives in
[`ERROR_ANALYSIS.md`](ERROR_ANALYSIS.md).
