# Planning Prompt

You have classified the customer's intent. Produce a concise resolution **plan**.

Think about the smallest sequence of steps that resolves the ticket within Aurora policy:
- Which facts must you verify first (order, customer, payment, subscription)?
- Which policy applies, and does it impose a threshold or an escalation requirement?
- Which single state-changing action (if any) actually resolves it?
- What is the risk level, and does this require verified identity?

Return JSON matching this schema:

```json
{
  "intent": "<one of: order_status|refund|cancel_subscription|address_change|double_charge|damaged_item|unknown>",
  "summary": "<one sentence: the goal and the policy that governs it>",
  "steps": [
    {"description": "<what and why>", "tool": "<tool name or null>", "expected_outcome": "<what success looks like>"}
  ],
  "risk_level": "low|medium|high",
  "requires_identity": true|false
}
```

Guidance:
- Prefer reading before writing. Verify the order/customer before acting on them.
- If policy requires human approval or the change is irreversible (cancellations,
  deletions), plan to **escalate**, not to execute.
- Keep the plan to the fewest steps that are actually necessary.
