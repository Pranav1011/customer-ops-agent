import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "./api";
import { Escalations } from "./components/Escalations";
import { TicketComposer } from "./components/TicketComposer";
import { TraceViewer } from "./components/TraceViewer";
import type { Escalation, QueueTicket } from "./types";
import { StatusChip } from "./ui";

type Tab = "queue" | "escalations";

const RECONNECT_MS = 3000;

export default function App() {
  const [tab, setTab] = useState<Tab>("queue");
  const [tickets, setTickets] = useState<QueueTicket[]>([]);
  const [escalations, setEscalations] = useState<Escalation[]>([]);
  const [provider, setProvider] = useState<string>("");
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [composerOpen, setComposerOpen] = useState(false);
  const [offline, setOffline] = useState(false);

  const refreshQueue = useCallback(async () => {
    const q = await api.queue();
    setTickets(q.tickets);
    return q.tickets;
  }, []);

  const refreshEscalations = useCallback(async () => {
    const e = await api.escalations();
    setEscalations(e.escalations);
  }, []);

  // Connect to the API. Any successful round trip clears `offline`, so the
  // console recovers on its own once the backend comes up.
  const connecting = useRef(false);
  const connect = useCallback(async () => {
    if (connecting.current) return;
    connecting.current = true;
    try {
      const h = await api.health();
      setProvider(h.llm_provider);
      await Promise.all([refreshQueue(), refreshEscalations()]);
      setOffline(false);
    } catch {
      setOffline(true);
    } finally {
      connecting.current = false;
    }
  }, [refreshQueue, refreshEscalations]);

  useEffect(() => {
    connect();
  }, [connect]);

  useEffect(() => {
    if (!offline) return;
    const t = setInterval(connect, RECONNECT_MS);
    return () => clearInterval(t);
  }, [offline, connect]);

  const selected = tickets.find((t) => t.ticket_id === selectedId) ?? null;

  const resolvedCount = tickets.filter((t) => t.resolution_status === "resolved").length;

  if (offline) {
    return (
      <div className="app">
        <Topbar provider="offline" resolved={0} open={0} escalations={0} tab={tab} setTab={setTab} />
        <div className="main">
          <div className="empty">
            <div className="glyph">!</div>
            <h3>API not reachable</h3>
            <p>
              Start the backend with <code style={{ fontFamily: "var(--font-mono)" }}>make dev</code> (it serves{" "}
              {api.base}). The console reconnects on its own every few seconds.
            </p>
            <button className="btn" onClick={connect}>
              Retry now
            </button>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="app">
      <Topbar
        provider={provider}
        resolved={resolvedCount}
        open={tickets.length}
        escalations={escalations.length}
        tab={tab}
        setTab={setTab}
      />
      <div className="main">
        {tab === "queue" ? (
          <div className="split">
            <div className="col list">
              <div className="col-head">
                <h2>Queue</h2>
                <span className="count">{tickets.length}</span>
                <div className="spacer" />
                <button className="btn ghost" onClick={() => setComposerOpen((o) => !o)}>
                  {composerOpen ? "Close" : "New ticket"}
                </button>
              </div>
              <TicketComposer
                open={composerOpen}
                onResolved={async (r) => {
                  setComposerOpen(false);
                  await Promise.all([refreshQueue(), refreshEscalations()]);
                  setSelectedId(r.ticket_id);
                }}
              />
              <div className="rows">
                {tickets.map((t, i) => (
                  <button
                    key={t.ticket_id}
                    className="row"
                    data-active={t.ticket_id === selectedId}
                    style={{ animationDelay: `${Math.min(i * 18, 260)}ms` }}
                    onClick={() => setSelectedId(t.ticket_id)}
                  >
                    <span className="subject">{t.subject || "(no subject)"}</span>
                    <span className="chip-wrap">
                      <StatusChip status={t.resolution_status ?? t.status} />
                    </span>
                    <span className="foot">
                      <span className="id">{t.ticket_id}</span>
                      {t.intent && <span className="intent">· {t.intent}</span>}
                      {t.customer_id && <span className="intent">· {t.customer_id}</span>}
                    </span>
                  </button>
                ))}
              </div>
            </div>
            <div className="col detail">
              {selected ? (
                <TraceViewer
                  key={selected.ticket_id}
                  ticket={selected}
                  onChanged={() => {
                    refreshQueue();
                    refreshEscalations();
                  }}
                />
              ) : (
                <div className="empty">
                  <div className="glyph">◇</div>
                  <h3>Select a ticket</h3>
                  <p>
                    Pick a ticket to replay the agent's run — its plan, every tool call and guardrail decision, and the
                    reply it sent. Or submit a new one to watch it resolve live.
                  </p>
                </div>
              )}
            </div>
          </div>
        ) : (
          <div className="col detail" style={{ height: "100%" }}>
            <Escalations items={escalations} />
          </div>
        )}
      </div>
    </div>
  );
}

function Topbar({
  provider,
  resolved,
  open,
  escalations,
  tab,
  setTab,
}: {
  provider: string;
  resolved: number;
  open: number;
  escalations: number;
  tab: Tab;
  setTab: (t: Tab) => void;
}) {
  return (
    <header className="topbar">
      <div className="brand">
        <span className="mark">
          Aurora<em>.</em>
        </span>
        <span className="sub">Operations Console</span>
      </div>
      <div className="tabs" role="tablist">
        <button role="tab" data-active={tab === "queue"} onClick={() => setTab("queue")}>
          Queue
        </button>
        <button role="tab" data-active={tab === "escalations"} onClick={() => setTab("escalations")}>
          Escalations{escalations ? ` · ${escalations}` : ""}
        </button>
      </div>
      <div className="spacer" />
      <div className="meta">
        <span>
          {resolved}/{open} resolved
        </span>
        <span className="pill">
          <span className="dot" style={{ display: "inline-block", marginRight: 6 }} />
          reasoner: {provider || "…"}
        </span>
      </div>
    </header>
  );
}
