"""LLM-as-judge for reply quality — and, crucially, a study that *validates the
judge itself* (per Shi et al., arXiv:2406.07791).

A judge you have validated is far more credible than one you just trust. We
measure:
  * position consistency — in a pairwise comparison, does the judge pick the
    better reply regardless of whether it's shown first (A) or second (B)?
  * repetition stability — does the judge return the same verdict when asked the
    same thing twice?
  * human agreement — on a hand-labeled subset, does the judge agree with the
    human label?
"""

from __future__ import annotations

from typing import Any

from agent_ops.llm.base import LLMProvider

# A deliberately poor reply used as the losing candidate in the pairwise study.
_POOR_REPLY = "No."


def build_rubric(scenario: dict[str, Any]) -> str:
    return (
        "You are grading an Aurora customer-support reply. A good reply is warm, "
        "concise, specific, grounded in real order facts, and sets correct expectations "
        "(including if the issue was escalated to a human). It must not leak internal "
        "details. Grade for this customer request: "
        f"'{scenario.get('request', '')}'."
    )


def score(
    provider: LLMProvider, scenario: dict[str, Any], reply: str
) -> tuple[bool, dict[str, Any]]:
    rubric = build_rubric(scenario)
    result = provider.score_reply(rubric, reply)
    return result.verdict == "pass", {"score": result.score, "verdict": result.verdict}


def run_validation(provider: LLMProvider, samples: list[dict[str, Any]]) -> dict[str, Any]:
    """samples: [{scenario, reply, human_label?}]. Returns judge reliability
    metrics across the samples."""
    pos_consistent = 0
    rep_stable = 0
    agree = 0
    labeled = 0

    for sample in samples:
        scenario = sample["scenario"]
        reply = sample["reply"]
        rubric = build_rubric(scenario)

        # Position consistency: the good reply should win whether shown as A or B.
        w1, _ = provider.compare(rubric, reply, _POOR_REPLY)  # good is A
        w2, _ = provider.compare(rubric, _POOR_REPLY, reply)  # good is B
        if w1 == "A" and w2 == "B":
            pos_consistent += 1

        # Repetition stability.
        v1 = provider.score_reply(rubric, reply).verdict
        v2 = provider.score_reply(rubric, reply).verdict
        if v1 == v2:
            rep_stable += 1

        # Human agreement (only where a label exists).
        label = sample.get("human_label")
        if label in ("pass", "fail"):
            labeled += 1
            if v1 == label:
                agree += 1

    n = max(1, len(samples))
    return {
        "samples": len(samples),
        "position_consistency": round(pos_consistent / n, 3),
        "repetition_stability": round(rep_stable / n, 3),
        "human_labeled": labeled,
        "human_agreement": round(agree / labeled, 3) if labeled else None,
    }
