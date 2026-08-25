import { useEffect, useState } from "react";
import { api } from "../api";
import type { QueueTicket, Trace, TraceEvent } from "../types";
import { StatusChip, viewEvent } from "../ui";

function fmtLatency(ms: number): React.ReactNode {
  if (ms >= 1000) return <>{(ms / 1000).toFixed(1)}<small> s</small></>;
  return <>{Math.round(ms)}<small> ms</small></>;
}

function Code({ value }: { value: unknown }) {
  if (value == null) return null;
  return <pre className="code">{JSON.stringify(value, null, 2)}</pre>;
}

function EventRow({ ev }: { ev: TraceEvent }) {
  const v = viewEvent(ev);
  return (
    <div className="ev" style={{ ["--tone" as string]: v.tone }}>
      <div className="ev-card">
        <div className="ev-top">
          <span className="ev-kind">{v.kind}</span>
          <span className="ev-title">{v.title}</span>
        </div>
        {v.note && <div className="ev-note">{v.note}</div>}
        {v.payload != null && (
          <details className="payload">
            <summary>payload</summary>
            <Code value={v.payload} />
          </details>
        )}
      </div>
    </div>
  );
}

function Metric({ k, children }: { k: string; children: React.ReactNode }) {
  return (
    <div className="metric">
      <div className="k">{k}</div>
      <div className="v">{children}</div>
    </div>
  );
}

export function TraceViewer({ ticket, onChanged }: { ticket: QueueTicket; onChanged: () => void }) {
  const [trace, setTrace] = useState<Trace | null>(null);
  const [loading, setLoading] = useState(false);
  const [running, setRunning] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    setTrace(null);
    setErr(null);
    if (!ticket.run_id) return;
    setLoading(true);
    api
      .trace(ticket.run_id)
      .then((t) => alive && setTrace(t))
      .catch((e) => alive && setErr((e as Error).message))
      .finally(() => alive && setLoading(false));
    return () => {
      alive = false;
    };
  }, [ticket.run_id, ticket.ticket_id]);

  async function runAgent() {
    setRunning(true);
    setErr(null);
    try {
      const r = await api.resolve(ticket.ticket_id);
      const t = await api.trace(r.run_id);
      setTrace(t);
      onChanged();
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setRunning(false);
    }
  }

  const summary = trace?.summary ?? {};
  const num = (k: string) => (summary[k] as number) ?? 0;

  return (
    <div className="detail-pad">
      <div className="detail-head">
        <div>
          <div className="id">{ticket.ticket_id}</div>
          <h1>{ticket.subject || (trace?.intent ?? "Ticket")}</h1>
        </div>
        <div className="spacer" />
        <button className="btn ghost" onClick={runAgent} disabled={running} title="Run the agent again on the current brain">
          {running ? "Reasoning…" : "Re-run live"}
        </button>
        <StatusChip status={trace?.status ?? ticket.resolution_status ?? ticket.status} />
      </div>

      {running && (
        <div className="running-banner">
          <span className="spin" /> Agent is reasoning over this ticket — with a local LLM this
          takes ~30–60s (it's actually thinking, not replaying).
        </div>
      )}

      {err && <div style={{ color: "var(--danger)", fontSize: 13, marginTop: 12 }}>{err}</div>}

      {!ticket.run_id && !trace ? (
        <div className="empty" style={{ minHeight: 300 }}>
          <div className="glyph">⁂</div>
          <h3>Not yet run</h3>
          <p>This ticket is still in the queue. Run the agent to watch it plan, act, and resolve.</p>
          <button className="btn" onClick={runAgent} disabled={running}>
            {running ? "Running agent…" : "Run agent"}
          </button>
        </div>
      ) : loading ? (
        <div className="skeleton">loading trace…</div>
      ) : trace ? (
        <>
          <div className="request">
            <div className="who">{trace.customer_id ?? "customer"} · request</div>
            {trace.request_text}
          </div>

          {trace.plan && "steps" in trace.plan && (
            <>
              <div className="section-label">Plan</div>
              <div className="plan">
                {trace.plan.steps.map((s, i) => (
                  <div className="step" key={i}>
                    <span className="n">{String(i + 1).padStart(2, "0")}</span>
                    <span>{s.description}</span>
                    <span className="tool">{s.tool ?? "—"}</span>
                  </div>
                ))}
              </div>
            </>
          )}

          <div className="section-label">Trajectory</div>
          <div className="timeline">
            {trace.events.map((ev) => (
              <EventRow ev={ev} key={ev.seq} />
            ))}
          </div>

          {trace.resolution?.customer_reply && (
            <>
              <div className="section-label">Customer reply</div>
              <div className="reply">
                <div className="who">Aurora → customer</div>
                {trace.resolution.customer_reply}
              </div>
            </>
          )}

          <div className="metrics">
            <Metric k="Reasoner">
              <span className="chip plain">{(summary.provider as string) ?? "—"}</span>
            </Metric>
            <Metric k="Outcome">
              <StatusChip status={trace.status} />
            </Metric>
            <Metric k="Tool calls">{num("tool_calls")}</Metric>
            <Metric k="Iterations">{num("iterations")}</Metric>
            <Metric k="Tokens">{num("total_tokens").toLocaleString()}</Metric>
            <Metric k="Cost">${num("total_cost_usd").toFixed(4)}</Metric>
            <Metric k="Latency">{fmtLatency(num("total_latency_ms"))}</Metric>
          </div>
        </>
      ) : null}
    </div>
  );
}
