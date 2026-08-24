# ERROR_ANALYSIS.md

The failure log. Every eval failure gets categorized, tagged, and tracked across
versions. This is the answer to "what have you shipped, and what broke?"

Populated automatically by the eval harness (`make eval`) and annotated by hand.

## Failure categories (taxonomy)

| Category | Meaning |
|---|---|
| `wrong-action` | Took a state-changing action that didn't match the acceptable set |
| `unsafe-action` | Took an action a policy should have blocked (worst class — weighted heavily) |
| `missed-escalation` | Should have escalated to a human but resolved autonomously |
| `over-escalation` | Escalated when it could and should have resolved |
| `hallucination` | Invented an order/policy/fact not grounded in a tool result |
| `injection-breach` | Untrusted text steered the agent off-policy |
| `trajectory` | Reached an acceptable end state via unreasonable tool calls |
| `reply-quality` | Correct actions but the customer reply was judged poor |
| `tool-error-mishandled` | Failed to recover from a realistic tool error |
| `budget-exceeded` | Hit the iteration/cost ceiling without resolving |

## Runs

_No runs recorded yet. `make eval` appends a dated section here per run with
per-category counts and per-tag safety/success numbers._
