"""The eval harness. `make eval` runs this.

For each golden scenario it builds a clean, known backend state, runs the agent
end to end, then scores task success, trajectory, action safety, and efficiency.
It also validates the LLM-as-judge and appends an error-analysis section.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import yaml

from agent_ops.config import REPO_ROOT

_DATASET = Path(__file__).resolve().parent / "dataset" / "scenarios.yaml"


def _configure_eval_env() -> None:
    """Point storage at eval-only locations so the harness never touches the
    developer's seeded dev database, and ensure the KB is indexed."""
    os.environ.setdefault("LLM_PROVIDER", "mock")
    os.environ.setdefault("DB_PATH", "data/eval.db")
    os.environ.setdefault("TRACE_DIR", "data/eval_traces")

    from agent_ops.config import get_settings

    get_settings.cache_clear()

    import agent_ops.backend.db as db

    db._engine = None
    db.init_db()

    # Index the policy KB so search_knowledge_base works during eval.
    from agent_ops.backend import kb
    from agent_ops.backend.kb_content import KB_ARTICLES

    if kb.get_collection().count() == 0:
        kb.index_articles(KB_ARTICLES)


def load_scenarios(path: Path = _DATASET) -> list[dict[str, Any]]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return data["scenarios"]


def run_one(scenario: dict[str, Any]) -> dict[str, Any]:
    from agent_ops.agent.graph import run_ticket
    from agent_ops.eval import judge, metrics
    from agent_ops.eval.fixtures import build_scenario_state
    from agent_ops.llm.router import get_provider

    build_scenario_state(scenario.get("setup", {}))
    customer_id = (scenario.get("setup", {}).get("customer", {}) or {}).get("id")

    result = run_ticket(
        scenario["request"],
        ticket_id=scenario["id"],
        customer_id=customer_id,
        order_id=scenario.get("order_id"),
    )
    trace = json.loads(Path(result["trace_path"]).read_text())

    judge_pass = None
    if (scenario.get("expect", {}) or {}).get("judge", True):
        judge_pass, _ = judge.score(get_provider(), scenario, result["customer_reply"])

    record = metrics.evaluate(scenario, result, trace, judge_pass)
    record["reply"] = result["customer_reply"]
    return record


def _rate(items: list[bool]) -> float:
    return round(sum(items) / len(items), 4) if items else 1.0


def aggregate(
    records: list[dict[str, Any]], validation: dict[str, Any], provider: str
) -> dict[str, Any]:
    n = len(records)
    costs = [r["efficiency"]["cost_usd"] or 0.0 for r in records]
    lat = [r["efficiency"]["latency_ms"] or 0.0 for r in records]
    toks = [r["efficiency"]["tokens"] or 0 for r in records]

    by_tag: dict[str, dict[str, Any]] = {}
    for r in records:
        for tag in r["tags"]:
            b = by_tag.setdefault(tag, {"success": [], "safe": []})
            b["success"].append(r["success"])
            b["safe"].append(r["safe"])
    by_tag_out = {
        tag: {
            "n": len(b["success"]),
            "success_rate": _rate(b["success"]),
            "safety_rate": _rate(b["safe"]),
        }
        for tag, b in by_tag.items()
    }

    categories: dict[str, int] = {}
    for r in records:
        if not r["success"]:
            categories[r["failure_category"]] = categories.get(r["failure_category"], 0) + 1

    critical = {}
    for tag in ("should-escalate", "injection", "cross-customer"):
        subset = [r["safe"] for r in records if tag in r["tags"]]
        if subset:
            critical[tag] = _rate(subset)

    return {
        "provider": provider,
        "overall": {
            "n": n,
            "success_rate": _rate([r["success"] for r in records]),
            "safety_rate": _rate([r["safe"] for r in records]),
            "avg_cost_usd": round(sum(costs) / n, 6) if n else 0.0,
            "avg_latency_ms": round(sum(lat) / n, 1) if n else 0.0,
            "avg_tokens": round(sum(toks) / n, 1) if n else 0.0,
        },
        "by_tag": by_tag_out,
        "failure_categories": categories,
        "safety_by_critical_tag": critical,
        "judge_validation": validation,
    }


def run(write: bool = True) -> dict[str, Any]:
    from agent_ops.config import get_settings
    from agent_ops.eval import judge
    from agent_ops.llm.router import get_provider

    _configure_eval_env()
    scenarios = load_scenarios()
    records = [run_one(s) for s in scenarios]

    # Judge validation over a hand-labeled sample (scenarios carrying human_label).
    samples = []
    for r, s in zip(records, scenarios, strict=True):
        samples.append({"scenario": s, "reply": r["reply"], "human_label": s.get("human_label")})
    validation = judge.run_validation(get_provider(), samples)

    agg = aggregate(records, validation, get_settings().llm_provider)

    from agent_ops.eval import report

    report.print_report(agg)
    if write:
        path = report.write_results(agg, records)
        report.append_error_analysis(agg, records)
        report._console.print(
            f"\nResults written to [cyan]{path.relative_to(REPO_ROOT)}[/] and ERROR_ANALYSIS.md"
        )
    return agg


def main() -> None:
    run(write=True)


if __name__ == "__main__":
    main()
