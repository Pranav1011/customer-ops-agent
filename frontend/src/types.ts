export type Status = "resolved" | "escalated" | "open" | "failed" | null;

export interface QueueTicket {
  ticket_id: string;
  customer_id: string | null;
  subject: string;
  status: string;
  intent: string | null;
  resolution_status: string | null;
  run_id: string | null;
}

export interface TicketResult {
  ticket_id: string;
  run_id: string;
  intent: string | null;
  status: string | null;
  escalated: boolean;
  customer_reply: string;
  escalation_reason: string | null;
  summary: Record<string, unknown>;
}

export interface TraceEvent {
  seq: number;
  type: string;
  ts?: string;
  tool?: string;
  args?: Record<string, unknown>;
  result?: { ok?: boolean; data?: unknown; error?: string | null };
  action?: string;
  rationale?: string;
  reason?: string;
  rule?: string | null;
  effect?: string;
  decision?: string;
  markers?: string[];
  confidence?: number;
  intent?: string;
  plan?: PlanShape;
  memory?: unknown;
  status?: string;
  customer_reply?: string;
  blocked?: boolean;
  entries_compacted?: number;
  order_id?: string | null;
  entry?: unknown;
}

export interface PlanStep {
  description: string;
  tool: string | null;
  expected_outcome?: string;
}
export interface PlanShape {
  intent: string;
  summary: string;
  steps: PlanStep[];
  risk_level: string;
  requires_identity: boolean;
}

export interface Trace {
  run_id: string;
  ticket_id: string | null;
  customer_id: string | null;
  intent: string | null;
  request_text: string;
  status: string | null;
  created_at: string;
  plan: PlanShape | Record<string, never>;
  events: TraceEvent[];
  usage: Array<Record<string, number | string>>;
  resolution: { status?: string; customer_reply?: string; escalation_reason?: string | null } | null;
  summary: Record<string, number | string | boolean | null>;
}

export interface Escalation {
  id: number;
  ticket_id: string | null;
  customer_id: string | null;
  run_id: string | null;
  reason: string;
  context: Record<string, unknown>;
  status: string;
  created_at: string;
}
