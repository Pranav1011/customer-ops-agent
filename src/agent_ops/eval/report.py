"""Rendering + persistence for eval results: a console report, a results JSON,
and the appended ERROR_ANALYSIS.md section."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.table import Table

from agent_ops.config import REPO_ROOT

_console = Console()


def print_report(agg: dict[str, Any]) -> None:
    o = agg["overall"]
    _console.rule("[bold]Aurora Eval Report")
    _console.print(
        f"provider=[cyan]{agg['provider']}[/] scenarios=[cyan]{o['n']}[/]  "
        f"task_success=[bold green]{o['success_rate']:.0%}[/]  "
        f"action_safety=[bold]{o['safety_rate']:.0%}[/]  "
        f"avg_cost=${o['avg_cost_usd']:.4f}  avg_latency={o['avg_latency_ms']:.0f}ms  "
        f"avg_tokens={o['avg_tokens']:.0f}"
    )

    t = Table(title="By tag", show_lines=False)
    t.add_column("tag")
    t.add_column("n", justify="right")
    t.add_column("success", justify="right")
    t.add_column("safety", justify="right")
    for tag, s in sorted(agg["by_tag"].items()):
        t.add_row(tag, str(s["n"]), f"{s['success_rate']:.0%}", f"{s['safety_rate']:.0%}")
    _console.print(t)

    if agg["failure_categories"]:
        ft = Table(title="Failure categories")
        ft.add_column("category")
        ft.add_column("count", justify="right")
        for cat, c in sorted(agg["failure_categories"].items(), key=lambda kv: -kv[1]):
            ft.add_row(cat, str(c))
        _console.print(ft)
    else:
        _console.print("[green]No failures.[/]")

    v = agg["judge_validation"]
    _console.print(
        f"\n[bold]Judge validation[/] (n={v['samples']}): "
        f"position_consistency={v['position_consistency']:.0%}  "
        f"repetition_stability={v['repetition_stability']:.0%}  "
        f"human_agreement="
        + (
            f"{v['human_agreement']:.0%} (n={v['human_labeled']})"
            if v["human_agreement"] is not None
            else "n/a"
        )
    )

    st = agg["safety_by_critical_tag"]
    for tag, rate in st.items():
        color = "green" if rate == 1.0 else "red"
        _console.print(f"critical safety ({tag}): [{color}]{rate:.0%}[/]")


def write_results(agg: dict[str, Any], records: list[dict[str, Any]]) -> Path:
    out_dir = REPO_ROOT / "eval_results"
    out_dir.mkdir(parents=True, exist_ok=True)
    payload = {"generated_at": datetime.now(UTC).isoformat(), "aggregate": agg, "records": records}
    (out_dir / "latest.json").write_text(
        json.dumps(payload, indent=2, default=str), encoding="utf-8"
    )
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = out_dir / f"run-{stamp}.json"
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return path


def append_error_analysis(agg: dict[str, Any], records: list[dict[str, Any]]) -> None:
    o = agg["overall"]
    failures = [r for r in records if not r["success"]]
    lines = [
        f"\n## Run {datetime.now(UTC).strftime('%Y-%m-%d %H:%M UTC')} — provider={agg['provider']}\n",
        f"- scenarios: **{o['n']}** · task success: **{o['success_rate']:.0%}** · "
        f"action safety: **{o['safety_rate']:.0%}** · avg cost: ${o['avg_cost_usd']:.4f} · "
        f"avg latency: {o['avg_latency_ms']:.0f}ms",
        f"- judge validation: position_consistency {agg['judge_validation']['position_consistency']:.0%}, "
        f"repetition_stability {agg['judge_validation']['repetition_stability']:.0%}",
        "",
        "| category | count |",
        "|---|---|",
    ]
    if agg["failure_categories"]:
        for cat, c in sorted(agg["failure_categories"].items(), key=lambda kv: -kv[1]):
            lines.append(f"| {cat} | {c} |")
    else:
        lines.append("| _none_ | 0 |")
    if failures:
        lines.append("\nFailing scenarios:")
        for r in failures:
            notes = "; ".join(r.get("safety_notes", []) + r.get("state_failures", []))
            lines.append(f"- `{r['id']}` [{r['failure_category']}] {notes}")
    lines.append("")

    path = REPO_ROOT / "ERROR_ANALYSIS.md"
    with path.open("a", encoding="utf-8") as f:
        f.write("\n".join(lines))
