"""Action claims: a customer reply may only say an action is done if this run did it.

Used by the resolve node. A real model writes its own reply, and on an escalated
run it can still tell the customer "we've processed your refund" when no refund
was written. Each completed-action phrase below maps to the write tool that has
to have succeeded in this run for the claim to be true.

What counts as a claim: past, perfect or in-progress wording ("we refunded",
"has been cancelled", "we're processing a refund"), and promises that money is
already moving ("your refund is on its way", "you'll see the refund in 3-5 days").
What does not: future and conditional wording ("a specialist will review your
refund request", "once the refund has been processed"), and a sentence that
describes earlier account history when this run's read results show that history
(a refunded payment or order, a cancelled subscription).
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Any

_ADV = r"(?:(?:just|now|already|also|successfully|(?:went|gone) ahead and)\s+)?"
# "we refunded", "we've refunded", "I have just refunded", "I went ahead and refunded".
_WE = r"\b(?:we|i)(?:'ve| have)?\s+" + _ADV
# "we're processing your refund" tells the customer the action is underway: a claim too.
_WE_NOW = r"\b(?:we|i)(?:'re|'m| are| am)\s+(?:(?:now|currently|already)\s+)?"
_DONE = r"\s+(?:(?:has|have)\s+(?:now\s+)?(?:already\s+)?(?:successfully\s+)?been|was|were)\s+"
_BEING = r"\s+(?:is|are)\s+(?:now\s+|currently\s+)?being\s+"
# An optional article, amount and qualifier before the noun: "a $20 full refund".
_ART = r"(?:you\s+)?(?:a|an|the|your|that|this)?\s*(?:\$?[\d.,]+\s+)?(?:full\s+|partial\s+)?"
# Up to 40 characters within one sentence (a decimal point does not end it).
_GAP = r"(?:[^.!?\n]|\.(?=\d)){0,40}?"

_CLAIMS: dict[str, list[re.Pattern[str]]] = {
    "issue_refund": [
        re.compile(_WE + r"(?:refunded|reversed)\b"),
        re.compile(_WE + r"(?:issued|processed|initiated|sent|approved)\s+" + _ART + r"refund\b"),
        re.compile(
            _WE + r"processed\s+(?:a|the|your)\s+request\s+for\s+(?:a|the|your)?\s*refund\b"
        ),
        re.compile(
            r"\brefund\b"
            + _GAP
            + _DONE
            + r"(?:processed|issued|initiated|completed|approved|sent)\b"
        ),
        re.compile(r"\brefund\s+(?:processed|issued|approved|completed)\b"),
        re.compile(r"\b(?:has|have)\s+been\s+refunded\b"),
        re.compile(
            _WE_NOW
            + r"(?:refunding|(?:issuing|processing|initiating|sending)\s+"
            + _ART
            + r"refund\b)"
        ),
        re.compile(r"\brefund\b" + _GAP + _BEING + r"(?:processed|issued|initiated|sent)\b"),
        re.compile(r"\brefund\b" + _GAP + r"\s+(?:is|are)\s+on\s+(?:its|the)\s+way\b"),
        re.compile(
            r"\byou(?:'ll| will| should| can expect to)\s+(?:see|receive|get)\s+"
            + _ART
            + r"refund\b"
            + _GAP
            + r"\b(?:in|within)\s+(?:the\s+next\s+)?\d"
        ),
    ],
    "cancel_subscription": [
        re.compile(_WE + r"cancell?ed\b"),
        re.compile(r"\b(?:has|have)\s+(?:now\s+)?been\s+(?:successfully\s+)?cancell?ed\b"),
        re.compile(r"\bsubscription\b" + _GAP + r"\s+(?:was|were)\s+cancell?ed\b"),
        re.compile(r"\bcancellation\b" + _GAP + _DONE + r"(?:processed|confirmed|completed)\b"),
        re.compile(_WE_NOW + r"cancell?ing\s+(?:your|the)\s+subscription\b"),
    ],
    "update_shipping_address": [
        re.compile(
            _WE + r"(?:updated|changed)\s+(?:the|your)\s+(?:shipping\s+|delivery\s+)?address\b"
        ),
        re.compile(
            _WE_NOW
            + r"(?:updating|changing)\s+(?:the|your)\s+(?:shipping\s+|delivery\s+)?address\b"
        ),
        re.compile(r"\baddress\b" + _GAP + _DONE + r"(?:updated|changed)\b"),
    ],
    "apply_account_credit": [
        re.compile(_WE + r"credited\b"),
        re.compile(
            _WE + r"(?:applied|added|issued)\s+" + _ART + r"(?:account\s+|store\s+)?credit\b"
        ),
        re.compile(
            _WE_NOW + r"(?:applying|adding|issuing)\s+" + _ART + r"(?:account\s+|store\s+)?credit\b"
        ),
        re.compile(r"\bcredit\b" + _GAP + _DONE + r"(?:applied|added|issued)\b"),
    ],
}

# A completed-action phrase inside a conditional or temporal clause is not a claim:
# "once your refund has been processed, it posts in 5-10 days".
_CONDITIONAL = re.compile(r"\b(?:once|after|when|if|until|as soon as)\b")
# Wording that places the action in the account's past rather than in this run.
_HISTORY = re.compile(
    r"\b(?:previous|previously|earlier|already|as of|last (?:week|month|year|time)|ago)\b"
    r"|\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s+\d{1,2}\b"
    r"|\b\d{4}-\d{2}-\d{2}\b|\b\d{1,2}/\d{1,2}(?:/\d{2,4})?\b"
)
_SENTENCE_END = re.compile(r"[!?\n]|\.(?!\d)")


def _sentence(text: str, match: re.Match[str]) -> tuple[str, str]:
    """(the sentence up to the match, the whole sentence) around `match`."""
    starts = [m.end() for m in _SENTENCE_END.finditer(text, 0, match.start())]
    start = starts[-1] if starts else 0
    end = _SENTENCE_END.search(text, match.end())
    return text[start : match.start()], text[start : end.end() if end else len(text)]


def _dicts(value: Any) -> Iterator[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for v in value.values():
            yield from _dicts(v)
    elif isinstance(value, list):
        for v in value:
            yield from _dicts(v)


def _history(scratchpad: list[dict[str, Any]]) -> set[str]:
    """Write tools whose effect this run's successful read results show as already
    present on the account: a refunded payment or order, a cancelled subscription."""
    seen: set[str] = set()
    for entry in scratchpad:
        result = entry.get("result") or {}
        if entry.get("tool") in _CLAIMS or not result.get("ok"):
            continue
        for d in _dicts(result.get("data")):
            if d.get("status") == "refunded" or (d.get("refunded_amount") or 0) > 0:
                seen.add("issue_refund")
            if "subscription_id" in d and (
                d.get("status") in ("cancelled", "canceled") or d.get("cancelled_effective")
            ):
                seen.add("cancel_subscription")
    return seen


def _is_claim(text: str, match: re.Match[str], history_backed: bool) -> bool:
    before, sentence = _sentence(text, match)
    if _CONDITIONAL.search(before):
        return False
    return not (history_backed and _HISTORY.search(sentence))


def unbacked_action_claims(
    reply: str, actions_taken: list[str], scratchpad: list[dict[str, Any]] | None = None
) -> list[str]:
    """Return the write tool for every action `reply` claims that is not in
    `actions_taken` (the writes that succeeded in this run). A sentence that frames
    the action as account history only passes when `scratchpad` read results show it."""
    text = reply.lower().replace("’", "'")
    done = set(actions_taken)
    history = _history(scratchpad or [])
    unbacked: list[str] = []
    for tool, patterns in _CLAIMS.items():
        if tool in done:
            continue
        backed = tool in history
        if any(_is_claim(text, m, backed) for p in patterns for m in p.finditer(text)):
            unbacked.append(tool)
    return unbacked
