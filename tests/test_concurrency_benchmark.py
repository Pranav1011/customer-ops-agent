"""2f: the worker pool overlaps I/O-bound work instead of serializing it.

Enforces the concurrency claim in CI: with a simulated per-ticket LLM latency,
4 workers must clear the batch at least ~2x the throughput of 1 (ideal ~4x).
Kept small/fast so it's deterministic in CI.
"""

from __future__ import annotations

from agent_ops.bench import run_benchmark


def test_concurrency_speeds_up_throughput():
    res = run_benchmark(n=6, concurrencies=(1, 4), delay=0.04)
    assert res["speedup_vs_1"][4] >= 2.0, res
