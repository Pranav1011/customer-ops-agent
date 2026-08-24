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

export const api = {
  base: BASE,
  health: () => j<{ status: string; llm_provider: string }>("/health"),
  queue: (limit = 100) => j<{ count: number; tickets: QueueTicket[] }>(`/queue?limit=${limit}`),
  trace: (runId: string) => j<Trace>(`/trace/${runId}`),
  escalations: () => j<{ count: number; escalations: Escalation[] }>("/escalations"),
  submit: (payload: { body: string; customer_id?: string; order_id?: string; subject?: string }) =>
    j<TicketResult>("/tickets", { method: "POST", body: JSON.stringify(payload) }),
  resolve: (ticketId: string) =>
    j<TicketResult>(`/tickets/${ticketId}/resolve`, { method: "POST" }),
};
