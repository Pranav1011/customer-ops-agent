# Aurora — Customer Operations Agent

> A workflow-automation agent that takes a customer-operations ticket **end to end** —
> it plans, takes **real actions** across mock systems (refunds, cancellations, address
> changes, credits, escalations), guards against its own failures, and is measured by a
> **real eval + observability harness**. Not a RAG chatbot: the headline is
> *action-taking + reliability + measurement*.

**Status:** under active construction. Building the core (mock backend → agent loop →
actions + guardrails → memory → eval harness) first; CI thresholds, model comparison,
an MCP server, and a thin UI follow.

---

## Quickstart

```bash
make install     # uv sync (creates .venv, installs deps)
make seed        # generate the mock backend: SQLite data + Chroma KB
make dev         # run the API at http://127.0.0.1:8000 (docs at /docs)
make eval        # run the eval harness and print the report
make test        # run the test suite
```

Runs **fully offline** with a deterministic mock LLM — no API key needed. To switch to
real Claude reasoning, set `LLM_PROVIDER=anthropic` and `ANTHROPIC_API_KEY` (see
Configuration below).

## Configuration

Config is read from the environment (and an optional `.env` file at the repo root) with
sensible defaults, so `.env` is optional. Recognized variables:

| Variable | Default | Purpose |
|---|---|---|
| `LLM_PROVIDER` | `mock` | `mock` (offline, deterministic) or `anthropic` (real) |
| `ANTHROPIC_API_KEY` | — | required only when `LLM_PROVIDER=anthropic` |
| `MODEL_CLASSIFIER` | `claude-haiku-4-5-20251001` | intent classification (cheap/fast) |
| `MODEL_REASONER` | `claude-sonnet-5` | planning + reasoning |
| `MODEL_JUDGE` | `claude-sonnet-5` | LLM-as-judge in evals |
| `DB_PATH` | `data/aurora.db` | SQLite file |
| `CHROMA_PATH` | `data/chroma` | vector KB store |
| `TRACE_DIR` | `data/traces` | per-run JSON traces |
| `MAX_ITERATIONS` | `8` | agent tool-loop cap |
| `COST_CEILING_USD` | `0.50` | per-task budget ceiling |
| `REFUND_APPROVAL_THRESHOLD` | `100.0` | refunds above this require human approval |

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
