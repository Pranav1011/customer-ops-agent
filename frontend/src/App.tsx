import { useCallback, useEffect, useRef, useState, type KeyboardEvent } from "react";
import { api, useReachability } from "./api";
import { Escalations } from "./components/Escalations";
import { EMPTY_DRAFT, TicketComposer, type Draft } from "./components/TicketComposer";
import { TraceViewer } from "./components/TraceViewer";
import type { Escalation, QueueTicket } from "./types";
import { StatusChip } from "./ui";

type Tab = "queue" | "escalations";
const TABS: Tab[] = ["queue", "escalations"];

const RECONNECT_MS = 3000;
const HEARTBEAT_MS = 5000;

export default function App() {
  const [tab, setTab] = useState<Tab>("queue");
  const [tickets, setTickets] = useState<QueueTicket[]>([]);
  const [escalations, setEscalations] = useState<Escalation[]>([]);
  const [provider, setProvider] = useState<string>("");
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [composerOpen, setComposerOpen] = useState(false);
  const [draft, setDraft] = useState<Draft>(EMPTY_DRAFT);
  const [syncedAt, setSyncedAt] = useState<Date | null>(null);
  const offline = useReachability() === "offline";

  const refreshQueue = useCallback(async () => {
    const q = await api.queue();
    setTickets(q.tickets);
    setSyncedAt(new Date());
    return q.tickets;
  }, []);

  const refreshEscalations = useCallback(async () => {
    const e = await api.escalations();
    setEscalations(e.escalations);
  }, []);

  // (Re)load everything. Reachability lives in the api layer: a network error on
  // any request flips the console offline, and any HTTP response flips it back.
  const connecting = useRef(false);
  const connect = useCallback(async () => {
    if (connecting.current) return;
    connecting.current = true;
    try {
      const h = await api.health();
      setProvider(h.llm_provider);
      await Promise.all([refreshQueue(), refreshEscalations()]);
    } catch {
      // Offline is reported by useReachability; HTTP errors leave the last good data on screen.
    } finally {
      connecting.current = false;
    }
  }, [refreshQueue, refreshEscalations]);

  useEffect(() => {
    connect();
  }, [connect]);

  // Offline: retry every few seconds; the first answer brings the console back.
  // Online: a light heartbeat, so an outage shows even while nobody is clicking.
  useEffect(() => {
    if (offline) {
      const t = setInterval(connect, RECONNECT_MS);
      return () => clearInterval(t);
    }
    const t = setInterval(() => api.health().catch(() => undefined), HEARTBEAT_MS);
    return () => clearInterval(t);
  }, [offline, connect]);

  // Coming back online reloads the data that went stale during the outage.
  const wasOffline = useRef(false);
  useEffect(() => {
    if (offline) wasOffline.current = true;
    else if (wasOffline.current) {
      wasOffline.current = false;
      connect();
    }
  }, [offline, connect]);

  const selected = tickets.find((t) => t.ticket_id === selectedId) ?? null;

  // "← Queue" unmounts the focused button on narrow screens; put focus back on
  // the row the person came from.
  const returnTo = useRef<string | null>(null);
  useEffect(() => {
    if (selectedId != null || returnTo.current == null) return;
    const id = returnTo.current;
    returnTo.current = null;
    document.querySelector<HTMLElement>(`.row[data-ticket-id="${CSS.escape(id)}"]`)?.focus();
  }, [selectedId]);

  const resolvedCount = tickets.filter((t) => t.resolution_status === "resolved").length;

  const openTicket = (ticketId: string) => {
    setSelectedId(ticketId);
    setTab("queue");
  };

  if (offline && !syncedAt) {
    return (
      <div className="app">
        <Topbar provider="offline" counts={null} escalations={0} tab={tab} setTab={setTab} offline />
        <div className="main">
          <div className="empty">
            <div className="glyph" aria-hidden="true">!</div>
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
    <div className="app" data-offline={offline}>
      <Topbar
        provider={offline ? "offline" : provider}
        counts={{ resolved: resolvedCount, total: tickets.length }}
        escalations={escalations.length}
        tab={tab}
        setTab={setTab}
        offline={offline}
      />
      {offline && syncedAt && (
        <div className="offline-banner" role="status">
          <span className="dot" />
          <span>
            <strong>API not reachable.</strong> Showing data from {syncedAt.toLocaleTimeString()} — it may be out of
            date. Reconnecting every few seconds.
          </span>
          <button className="btn ghost" onClick={connect}>
            Retry now
          </button>
        </div>
      )}
      <div className="main" data-stale={offline} role="tabpanel" id={`panel-${tab}`} aria-labelledby={`tab-${tab}`}>
        {tab === "queue" ? (
          <div className="split" data-detail-open={selected != null}>
            <div className="col list">
              <div className="col-head">
                <h2>Queue</h2>
                <span className="count">{tickets.length}</span>
                <div className="spacer" />
                <button
                  className="btn ghost"
                  aria-expanded={composerOpen}
                  aria-controls="composer"
                  onClick={() => setComposerOpen((o) => !o)}
                >
                  {composerOpen ? "Close" : "New ticket"}
                </button>
              </div>
              <TicketComposer
                open={composerOpen}
                draft={draft}
                onDraft={setDraft}
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
                    data-ticket-id={t.ticket_id}
                    style={{ animationDelay: `${Math.min(i * 18, 260)}ms` }}
                    aria-current={t.ticket_id === selectedId ? "true" : undefined}
                    onClick={() => setSelectedId(t.ticket_id)}
                  >
                    <span className="subject">{t.subject || t.intent || "Untitled ticket"}</span>
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
                <>
                  <div className="back-bar">
                    <button
                      className="btn ghost"
                      onClick={() => {
                        returnTo.current = selectedId;
                        setSelectedId(null);
                      }}
                    >
                      ← Queue
                    </button>
                  </div>
                  <TraceViewer
                    key={selected.ticket_id}
                    ticket={selected}
                    onChanged={() => {
                      refreshQueue();
                      refreshEscalations();
                    }}
                  />
                </>
              ) : (
                <div className="empty">
                  <div className="glyph" aria-hidden="true">◇</div>
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
            <Escalations items={escalations} onResolved={refreshEscalations} onOpenTicket={openTicket} />
          </div>
        )}
      </div>
    </div>
  );
}

function Topbar({
  provider,
  counts,
  escalations,
  tab,
  setTab,
  offline = false,
}: {
  provider: string;
  counts: { resolved: number; total: number } | null;
  escalations: number;
  tab: Tab;
  setTab: (t: Tab) => void;
  offline?: boolean;
}) {
  // Arrow keys move between tabs (WAI-ARIA tabs pattern); Tab leaves the list.
  function onTabKey(e: KeyboardEvent<HTMLDivElement>) {
    const step = e.key === "ArrowRight" ? 1 : e.key === "ArrowLeft" ? -1 : 0;
    if (!step) return;
    e.preventDefault();
    const next = TABS[(TABS.indexOf(tab) + step + TABS.length) % TABS.length];
    setTab(next);
    document.getElementById(`tab-${next}`)?.focus();
  }

  return (
    <header className="topbar">
      <div className="brand">
        <span className="mark">
          Aurora<em>.</em>
        </span>
        <span className="sub">Operations Console</span>
      </div>
      <div className="tabs" role="tablist" aria-label="Console views" onKeyDown={onTabKey}>
        {TABS.map((t) => (
          <button
            key={t}
            role="tab"
            id={`tab-${t}`}
            aria-selected={tab === t}
            aria-controls={`panel-${t}`}
            tabIndex={tab === t ? 0 : -1}
            data-active={tab === t}
            onClick={() => setTab(t)}
          >
            {t === "queue" ? "Queue" : `Escalations${escalations ? ` · ${escalations}` : ""}`}
          </button>
        ))}
      </div>
      <div className="spacer" />
      <div className="meta">
        {counts && (
          <span>
            {counts.resolved} of {counts.total} resolved
          </span>
        )}
        <span className="pill" data-offline={offline}>
          <span className="dot" style={{ display: "inline-block", marginRight: 6 }} />
          reasoner: {provider || "…"}
        </span>
      </div>
    </header>
  );
}
