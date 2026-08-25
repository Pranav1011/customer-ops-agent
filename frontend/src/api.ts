import type { Escalation, QueueTicket, TicketResult, Trace } from "./types";

const BASE: string =
  (import.meta as unknown as { env: Record<string, string> }).env?.VITE_API ??
  "http://127.0.0.1:8000";

async function j<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    ...init,
    headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
  });
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
