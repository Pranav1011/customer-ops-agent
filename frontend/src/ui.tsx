import type { TraceEvent } from "./types";

export function StatusChip({ status }: { status: string | null | undefined }) {
  const s = (status ?? "").toLowerCase();
  const cls =
    s === "resolved" ? "ok" : s === "escalated" ? "warn" : s === "failed" ? "danger" : s === "open" ? "neutral" : "plain";
  return <span className={`chip ${cls}`}>{status ?? "—"}</span>;
}

const C = {
  ok: "var(--ok)",
  warn: "var(--warn)",
  danger: "var(--danger)",
  neutral: "var(--neutral)",
  faint: "var(--text-faint)",
} as const;

export interface EventView {
  tone: string;
  kind: string;
  title: string;
  note?: string;
  payload?: unknown;
}

/** Map a raw trace event to a legible timeline row. */
export function viewEvent(ev: TraceEvent): EventView {
  switch (ev.type) {
    case "intent":
      return {
        tone: C.neutral,
        kind: "intake",
        title: `intent · ${ev.intent}`,
        note: `confidence ${fmtConf(ev.confidence)}${ev.order_id ? ` · order ${ev.order_id}` : ""}`,
      };
    case "plan":
      return { tone: C.faint, kind: "plan", title: `${ev.plan?.steps?.length ?? 0} steps · risk ${ev.plan?.risk_level ?? "?"}`, note: ev.plan?.summary };
    case "memory_recall":
      return { tone: C.neutral, kind: "memory", title: "recall", note: memNote(ev.memory), payload: ev.memory };
    case "memory_write":
      return { tone: C.neutral, kind: "memory", title: "write episodic", payload: ev.entry };
    case "compaction":
      return { tone: C.faint, kind: "context", title: `compacted ${ev.entries_compacted} entries` };
    case "decision": {
      const escalating = ev.action === "escalate";
      return {
        tone: escalating ? C.warn : C.neutral,
        kind: "decision",
        title: ev.action === "call_tool" ? `call ${ev.tool}` : ev.action ?? "decide",
        note: ev.rationale,
        payload: ev.args && Object.keys(ev.args).length ? ev.args : undefined,
      };
    }
    case "tool_call": {
      const ok = ev.result?.ok;
      const blocked = ev.blocked;
      return {
        tone: blocked ? C.danger : ok ? C.ok : C.danger,
        kind: "tool",
        title: ev.tool ?? "tool",
        note: blocked ? "blocked by policy" : ok ? "ok" : (ev.result?.error ?? "error"),
        payload: { args: ev.args, result: ev.result },
      };
    }
    case "guard": {
      const effect = ev.effect ?? ev.decision;
      const tone = effect === "block" || ev.decision === "stop" ? C.danger : C.warn;
      return {
        tone,
        kind: "guardrail",
        title: guardTitle(ev),
        note: ev.reason,
        payload: ev.markers ? { markers: ev.markers } : undefined,
      };
    }
    case "escalation":
      return { tone: C.warn, kind: "escalate", title: "routed to human", note: ev.reason ?? undefined };
    case "reply":
      return { tone: ev.status === "resolved" ? C.ok : C.warn, kind: "resolve", title: ev.status ?? "reply" };
    default:
      return { tone: C.faint, kind: ev.type, title: ev.type };
  }
}

function guardTitle(ev: TraceEvent): string {
  if (ev.decision === "sanitize_input") return "sanitized untrusted input";
  if (ev.decision === "stop") return `budget stop · ${ev.reason ?? ""}`;
  if (ev.tool) return `${ev.effect} · ${ev.tool}`;
  return ev.rule ?? "policy";
}

function fmtConf(c?: number): string {
  return c == null ? "—" : `${Math.round(c * 100)}%`;
}

function memNote(mem: unknown): string | undefined {
  if (!mem || typeof mem !== "object") return undefined;
  const m = mem as { interaction_count?: number; returning_customer?: boolean };
  if (m.interaction_count == null) return undefined;
  return m.returning_customer ? `returning customer · ${m.interaction_count} prior` : "first interaction";
}
