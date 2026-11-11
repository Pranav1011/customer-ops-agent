import { useState } from "react";
import { api } from "../api";
import type { Escalation } from "../types";

export function Escalations({
  items,
  onResolved,
  onOpenTicket,
}: {
  items: Escalation[];
  onResolved: () => void;
  onOpenTicket: (ticketId: string) => void;
}) {
  if (items.length === 0) {
    return (
      <div className="empty">
        <div className="glyph" aria-hidden="true">
          ✓
        </div>
        <h3>Inbox clear</h3>
        <p>No open escalations. When the agent hands a ticket to a human, it lands here with full context.</p>
      </div>
    );
  }
  return (
    <div className="esc-list">
      {items.map((e) => (
        <EscalationCard key={e.id} e={e} onResolved={onResolved} onOpenTicket={onOpenTicket} />
      ))}
    </div>
  );
}

function EscalationCard({
  e,
  onResolved,
  onOpenTicket,
}: {
  e: Escalation;
  onResolved: () => void;
  onOpenTicket: (ticketId: string) => void;
}) {
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  async function resolve() {
    setBusy(true);
    setErr(null);
    try {
      await api.resolveEscalation(e.id);
      onResolved();
    } catch (x) {
      setErr((x as Error).message);
      setBusy(false);
    }
  }

  return (
    <article className="esc" aria-label={`Escalation for ${e.ticket_id ?? "unknown ticket"}`}>
      <div className="top">
        <span className="chip warn">escalated</span>
        <span className="reason">{e.reason}</span>
      </div>
      <div className="who">
        {e.ticket_id ? (
          <button type="button" className="link" onClick={() => onOpenTicket(e.ticket_id as string)}>
            {e.ticket_id}
          </button>
        ) : (
          "no ticket"
        )}{" "}
        · {e.customer_id ?? "no customer"} · {new Date(e.created_at).toLocaleString()}
      </div>
      <div className="esc-actions">
        {e.context && Object.keys(e.context).length > 0 && (
          <details className="payload">
            <summary>context</summary>
            <pre className="code">{JSON.stringify(e.context, null, 2)}</pre>
          </details>
        )}
        <button type="button" className="btn ghost resolve" onClick={resolve} disabled={busy}>
          {busy ? "Closing…" : "Mark handled"}
        </button>
      </div>
      {err && (
        <div className="error-text" role="alert">
          {err}
        </div>
      )}
    </article>
  );
}
