# Next-Action Prompt

Given the plan and the tool results gathered so far (the scratchpad), decide the single
next action. Do not repeat a tool call whose result you already have.

Return JSON matching this schema:

```json
{
  "action": "call_tool | finish | escalate",
  "tool": "<tool name, if action is call_tool or escalate>",
  "args": { "<arg>": "<value>" },
  "rationale": "<one sentence>",
  "confidence": 0.0
}
```

Rules:
- Choose `call_tool` to gather a missing fact or to take the resolving action.
- Choose `finish` only when you have enough grounded information to write the reply and
  any required action has been taken (or none is needed).
- Choose `escalate` when policy requires human approval, identity is unverified for a
  sensitive change, the request is ambiguous, or your confidence is low.
- Never invent tool arguments. Use ids exactly as they appear in prior results.
