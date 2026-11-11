"""Action claims: a customer reply may only say an action is done if this run did it.

Used by the resolve node. A real model writes its own reply, and on an escalated
run it can still tell the customer "we've processed your refund" when no refund
was written. Each completed-action phrase below maps to the write tool that has
to have succeeded in this run for the claim to be true.

Matching is deliberately narrow: past, perfect or in-progress wording ("we've
refunded", "has been cancelled", "we're processing a refund"). Future and conditional wording ("we will look into a
refund", "once the refund has been processed") is not a claim.
"""

from __future__ import annotations

import re

_WE = r"\b(?:we|i)(?:'ve| have)\s+(?:(?:just|now|already|also|successfully)\s+)?"
# "we're processing your refund" tells the customer the action is underway: a claim too.
_WE_NOW = r"\b(?:we|i)(?:'re|'m| are| am)\s+(?:(?:now|currently|already)\s+)?"
_BEING = r"\s+(?:is|are)\s+(?:now\s+|currently\s+)?being\s+"
_DONE = r"\s+(?:has|have)\s+(?:now\s+)?(?:already\s+)?(?:successfully\s+)?been\s+"

# Up to 40 characters within one sentence (a decimal point does not end it).
_GAP = r"(?:[^.!?\n]|\.(?=\d)){0,40}?"

_CLAIMS: dict[str, list[re.Pattern[str]]] = {
    "issue_refund": [
        re.compile(_WE + r"refunded\b"),
        re.compile(
            _WE + r"(?:issued|processed|initiated|sent)\s+(?:you\s+)?(?:a|the|your)?\s*"
            r"(?:full\s+|partial\s+)?refund\b"
        ),
        re.compile(
            _WE + r"processed\s+(?:a|the|your)\s+request\s+for\s+(?:a|the|your)?\s*refund\b"
        ),
        re.compile(
            r"\brefund\b"
            + _GAP
            + _DONE
            + r"(?:processed|issued|initiated|completed|approved|sent)\b"
        ),
        re.compile(r"\b(?:has|have)\s+been\s+refunded\b"),
        re.compile(
            _WE_NOW + r"(?:refunding|(?:issuing|processing|initiating|sending)\s+(?:you\s+)?"
            r"(?:a|the|your)?\s*(?:full\s+|partial\s+)?refund\b)"
        ),
        re.compile(r"\brefund\b" + _GAP + _BEING + r"(?:processed|issued|initiated|sent)\b"),
    ],
    "cancel_subscription": [
        re.compile(_WE + r"cancell?ed\b"),
        re.compile(_WE_NOW + r"cancell?ing\s+(?:your|the)\s+subscription\b"),
        re.compile(r"\b(?:has|have)\s+(?:now\s+)?been\s+(?:successfully\s+)?cancell?ed\b"),
        re.compile(r"\bcancellation\b" + _GAP + _DONE + r"(?:processed|confirmed|completed)\b"),
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
        re.compile(
            _WE + r"(?:applied|added|issued)\s+(?:you\s+)?(?:a|an|the)?\s*(?:\$?[\d.,]+\s+)?"
            r"(?:account\s+|store\s+)?credit\b"
        ),
        re.compile(
            _WE_NOW + r"(?:applying|adding|issuing)\s+(?:you\s+)?(?:a|an|the)?\s*(?:\$?[\d.,]+\s+)?"
            r"(?:account\s+|store\s+)?credit\b"
        ),
        re.compile(r"\bcredit\b" + _GAP + _DONE + r"(?:applied|added|issued)\b"),
    ],
}

# A completed-action phrase inside a conditional or temporal clause is not a claim:
# "once your refund has been processed, it posts in 5-10 days".
_CONDITIONAL = re.compile(r"\b(?:once|after|when|if|until|as soon as)\b")
_SENTENCE_START = re.compile(r"[!?\n]|\.(?!\d)")


def _is_claim(text: str, match: re.Match[str]) -> bool:
    starts = [m.end() for m in _SENTENCE_START.finditer(text, 0, match.start())]
    clause = text[starts[-1] if starts else 0 : match.start()]
    return not _CONDITIONAL.search(clause)


def unbacked_action_claims(reply: str, actions_taken: list[str]) -> list[str]:
    """Return the write tool for every completed action `reply` claims that is not in
    `actions_taken` (the writes that succeeded in this run)."""
    text = reply.lower().replace("’", "'")
    done = set(actions_taken)
    unbacked: list[str] = []
    for tool, patterns in _CLAIMS.items():
        if tool in done:
            continue
        if any(_is_claim(text, m) for p in patterns for m in p.finditer(text)):
            unbacked.append(tool)
    return unbacked
