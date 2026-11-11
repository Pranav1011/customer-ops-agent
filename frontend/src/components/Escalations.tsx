import { useId, useState } from "react";
import { api, HttpError } from "../api";
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

const TOKEN_KEY = "aurora.operatorToken";

function savedToken(): string {
  try {
    return sessionStorage.getItem(TOKEN_KEY) ?? "";
  } catch {
    return "";
  }
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
  const [closing, setClosing] = useState(false);
  const [note, setNote] = useState("");
  const [token, setToken] = useState(savedToken);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const ids = useId();
  const noteOk = note.trim().length >= 3;

  async function resolve() {
    setBusy(true);
    setErr(null);
    try {
      await api.resolveEscalation(e.id, note.trim(), token.trim());
      try {
        sessionStorage.setItem(TOKEN_KEY, token.trim());
      } catch {
        // Storage can be blocked; the token is then asked for again next time.
      }
      onResolved();
    } catch (x) {
      setErr((x as Error).message);
      // Someone else closed it first: show the inbox as it is now.
      if (x instanceof HttpError && x.status === 409) onResolved();
    } finally {
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
        {!closing && (
          <button type="button" className="btn ghost resolve" onClick={() => setClosing(true)}>
            Mark handled
          </button>
        )}
      </div>
      {closing && (
        <form
          className="close-form"
          onSubmit={(ev) => {
            ev.preventDefault();
            if (noteOk && !busy) resolve();
          }}
        >
          <div className="field">
            <label htmlFor={`${ids}-note`}>What did you do?</label>
            <textarea
              id={`${ids}-note`}
              className="textarea"
              autoFocus
              value={note}
              maxLength={500}
              onChange={(x) => setNote(x.target.value)}
              placeholder="e.g. Approved the $249 refund after checking photos"
            />
          </div>
          <div className="field">
            <label htmlFor={`${ids}-token`}>
              Operator token <span className="optional">the API's AURORA_OPERATOR_TOKEN</span>
            </label>
            <input
              id={`${ids}-token`}
              className="input"
              type="password"
              autoComplete="off"
              value={token}
              onChange={(x) => setToken(x.target.value)}
            />
          </div>
          <div className="actions">
            <button type="submit" className="btn" disabled={busy || !noteOk}>
              {busy ? "Closing…" : "Close escalation"}
            </button>
            <button type="button" className="btn ghost" onClick={() => setClosing(false)} disabled={busy}>
              Cancel
            </button>
          </div>
        </form>
      )}
      {err && (
        <div className="error-text" role="alert">
          {err}
        </div>
      )}
    </article>
  );
}
