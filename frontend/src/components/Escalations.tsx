import type { Escalation } from "../types";

export function Escalations({ items }: { items: Escalation[] }) {
  if (items.length === 0) {
    return (
      <div className="empty">
        <div className="glyph">✓</div>
        <h3>Inbox clear</h3>
        <p>No open escalations. When the agent hands a ticket to a human, it lands here with full context.</p>
      </div>
    );
  }
  return (
    <div className="esc-list">
      {items.map((e) => (
        <div className="esc" key={e.id}>
          <div className="top">
            <span className="chip warn">escalated</span>
            <span className="reason">{e.reason}</span>
          </div>
          <div className="who">
            {e.ticket_id ?? "—"} · {e.customer_id ?? "—"} · {new Date(e.created_at).toLocaleString()}
          </div>
          {e.context && Object.keys(e.context).length > 0 && (
            <details className="payload" style={{ marginTop: 10 }}>
              <summary>context</summary>
              <pre className="code">{JSON.stringify(e.context, null, 2)}</pre>
            </details>
          )}
        </div>
      ))}
    </div>
  );
}
