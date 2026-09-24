"""Concurrency benchmark.

Proves the worker pool processes I/O-bound tickets *concurrently*: with a
simulated per-call LLM latency, the wall-clock to drain N tickets drops ~linearly
as worker concurrency rises. This models real LLM I/O honestly — `time.sleep`
releases the GIL exactly as a blocking socket read (httpx) does, so overlapping
sleeps is the same thing threads do while waiting on the LLM.

We did NOT rewrite the stack to async (see ENGINEERING_LOG 2026-09-05): the sync
agent core on a worker pool already gives this concurrency; a half-async rewrite
over sync SQLite would block the event loop and be strictly worse.

Run:  python -m agent_ops.bench
"""

from __future__ import annotations

import json
import time
from contextlib import contextmanager
from datetime import date

from agent_ops.config import REPO_ROOT


def _drain(ids: list[str], timeout: float = 60.0) -> None:
    from agent_ops.api import worker

    deadline = time.time() + timeout
    while time.time() < deadline:
        if all((worker.get_job(i) or {}).get("status") in ("succeeded", "failed") for i in ids):
            return
        time.sleep(0.01)
    raise TimeoutError("jobs did not drain within timeout")


@contextmanager
def _simulated_latency(delay: float):
    """Add `delay` seconds to one LLM call per ticket (classify), to model
    I/O-bound work deterministically."""
    from agent_ops.llm import mock as m

    original = m.MockProvider.classify

    def slow(self, *args, **kwargs):
        time.sleep(delay)
        return original(self, *args, **kwargs)

    m.MockProvider.classify = slow  # type: ignore[method-assign]
    try:
        yield
    finally:
        m.MockProvider.classify = original  # type: ignore[method-assign]


def _run_one(n: int, concurrency: int) -> float:
    from agent_ops.api import worker
    from agent_ops.config import get_settings

    settings = get_settings()
    original = settings.worker_concurrency
    settings.worker_concurrency = concurrency
    worker._executor.cache_clear()  # rebuild the pool at the new size
    try:
        t0 = time.perf_counter()
        ids = [
            worker.enqueue(
                body="Where is my order ORD-000001?", ticket_id=f"TCK-BENCH-{concurrency}-{i}"
            )
            for i in range(n)
        ]
        _drain(ids)
        return round(time.perf_counter() - t0, 3)
    finally:
        settings.worker_concurrency = original
        worker._executor.cache_clear()


def run_benchmark(
    n: int = 8, concurrencies: tuple[int, ...] = (1, 2, 4), delay: float = 0.05
) -> dict:
    with _simulated_latency(delay):
        walls = {c: _run_one(n, c) for c in concurrencies}
    base = walls[min(concurrencies)]
    return {
        "n_tickets": n,
        "sim_latency_ms": int(delay * 1000),
        "wall_seconds": walls,
        "throughput_per_s": {c: round(n / w, 2) for c, w in walls.items()},
        "speedup_vs_1": {c: round(base / w, 2) for c, w in walls.items()},
    }


def main() -> None:
    from agent_ops.backend.seed import reset_and_seed

    reset_and_seed()
    res = run_benchmark(n=12, concurrencies=(1, 2, 4), delay=0.1)
    res["date"] = date.today().isoformat()
    out = REPO_ROOT / "benchmarks" / "results" / f"concurrency-{res['date']}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(res, indent=2))
    print(json.dumps(res, indent=2))
    print(f"\nwrote {out}")


if __name__ == "__main__":
    main()
