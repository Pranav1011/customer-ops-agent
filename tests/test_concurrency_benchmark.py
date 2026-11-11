"""2f: the worker pool overlaps I/O-bound work instead of serializing it.

Enforces the concurrency claim in CI: with a simulated per-ticket LLM latency,
4 workers must clear the batch at least ~2x the throughput of 1 (ideal ~4x).
Uses the same 100 ms simulated latency as the README benchmark: with a shorter
delay, fixed per-ticket CPU work dominates on slow CI runners and the test
measures the machine instead of the overlap.
"""

from __future__ import annotations

from agent_ops.bench import run_benchmark


def test_concurrency_speeds_up_throughput():
    res = run_benchmark(n=8, concurrencies=(1, 4), delay=0.1)
    assert res["speedup_vs_1"][4] >= 2.0, res
