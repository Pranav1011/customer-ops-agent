import { useSyncExternalStore } from "react";
import type { Escalation, QueueTicket, TicketResult, Trace } from "./types";

const BASE: string =
  (import.meta as unknown as { env: Record<string, string> }).env?.VITE_API ??
  "http://127.0.0.1:8000";

/** Whether the API answered the last request. Owned here; every request updates it. */
export type Reachability = "online" | "offline";

let reachability: Reachability = "online";
const listeners = new Set<() => void>();

function setReachability(next: Reachability) {
  if (next === reachability) return;
  reachability = next;
  listeners.forEach((l) => l());
}

function subscribe(listener: () => void) {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

export function useReachability(): Reachability {
  return useSyncExternalStore(subscribe, () => reachability);
}

/** The request never got an HTTP response: the API is down or unreachable. */
export class NetworkError extends Error {
  constructor(path: string) {
    super(`API not reachable — ${path}`);
    this.name = "NetworkError";
  }
}

async function j<T>(path: string, init?: RequestInit): Promise<T> {
  let res: Response;
  try {
    res = await fetch(`${BASE}${path}`, {
      ...init,
      headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
    });
  } catch {
    setReachability("offline");
    throw new NetworkError(path);
  }
  // Any HTTP response, error statuses included, means the API is up.
  setReachability("online");
  if (!res.ok) throw new Error(`${res.status} ${res.statusText} — ${path}`);
  return res.json() as Promise<T>;
}

interface JobRef {
  job_id: string;
  ticket_id: string;
  status: string;
}
interface JobState {
  status: "queued" | "running" | "succeeded" | "failed";
  error: string | null;
  result: TicketResult | null;
}

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

/** Poll an async job until it terminates, returning its result (or throwing). */
async function pollJob(jobId: string, { interval = 800, tries = 250 } = {}): Promise<TicketResult> {
  for (let i = 0; i < tries; i++) {
    const s = await j<JobState>(`/jobs/${jobId}`);
    if (s.status === "succeeded" && s.result) return s.result;
    if (s.status === "failed") throw new Error(s.error ?? "job failed");
    await sleep(interval);
  }
  throw new Error("job timed out");
}

export const api = {
  base: BASE,
  health: () => j<{ status: string; llm_provider: string }>("/health"),
  queue: (limit = 100) => j<{ count: number; tickets: QueueTicket[] }>(`/queue?limit=${limit}`),
  trace: (runId: string) => j<Trace>(`/trace/${runId}`),
  escalations: () => j<{ count: number; escalations: Escalation[] }>("/escalations"),
  resolveEscalation: (id: number) => j<Escalation>(`/escalations/${id}/resolve`, { method: "POST" }),
  metrics: () => j<Record<string, unknown>>("/metrics"),
  // Submit + re-run go through the async job queue (non-blocking) and poll to completion.
  submit: async (payload: { body: string; customer_id?: string; order_id?: string; subject?: string }) => {
    const ref = await j<JobRef>("/jobs", { method: "POST", body: JSON.stringify(payload) });
    return pollJob(ref.job_id);
  },
  resolve: async (ticketId: string) => {
    const ref = await j<JobRef>(`/tickets/${ticketId}/rerun`, { method: "POST" });
    return pollJob(ref.job_id);
  },
};
