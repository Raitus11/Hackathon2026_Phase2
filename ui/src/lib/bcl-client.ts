/**
 * BCL API client.
 *
 * Talks via /api/bcl/* which next.config.js proxies to http://localhost:8080.
 * The BCL must be running for any of these to return data.
 */

const BCL_BASE = "/api/bcl";

export type HealthStatus = "healthy" | "degraded" | "unhealthy";

export interface Health {
  status: HealthStatus;
  bcl_version: string;
  db_reachable: boolean;
  k8s_reachable: boolean;
  mq_reachable_count: number;
  mq_total_count: number;
  lamport_clock: number;
}

export interface QueueManager {
  id: number;
  qm_name: string;
  pod_name: string | null;
  service_name: string | null;
  listener_port: number;
  web_port: number;
  dlq_name: string;
  deployed_at: string | null;
  is_ready: boolean;
}

export interface Topology {
  id: number;
  name: string;
  kind: "SOURCE" | "TARGET";
  spec: Record<string, unknown>;
  created_at: string;
  queue_managers: QueueManager[];
}

export interface AuditEntry {
  id: number;
  lamport_clock: number;
  wall_clock: string;
  correlation_id: string;
  actor: string;
  operation: string;
  app_id: string | null;
  qm_name: string | null;
  success: boolean;
  error_message: string | null;
  is_rollback: boolean;
}

export interface AuditPage {
  entries: AuditEntry[];
  next_cursor: number | null;
  total_count: number | null;
}

// ────────── Provisioning ──────────

export type ProvisionState =
  | "PENDING"
  | "RUNNING"
  | "COMPLETED"
  | "FAILED";

export type ProvisionPhase =
  | "PERSISTENTVOLUMECLAIM_APPLY"
  | "SECRET_APPLY"
  | "DEPLOYMENT_APPLY"
  | "SERVICE_APPLY"
  | "WAIT_FOR_READY"
  | "COMPLETE"
  | "EXCEPTION";

export type ProvisionStatus =
  | "APPLYING"
  | "APPLIED"
  | "FAILED"
  | "DRY_RUN"
  | "WAITING"
  | "READY"
  | "TIMEOUT";

export interface ProvisionEvent {
  qm_name: string;
  phase: ProvisionPhase | string;
  status: ProvisionStatus | string;
  timestamp: string;
  error?: string;
  pod_name?: string;
}

export interface ProvisionRun {
  run_id: string;
  topology_id: number;
  state: ProvisionState;
  qms_total: number;
  qms_ready: number;
  qms_failed: number;
  started_at: string;
  finished_at: string | null;
  correlation_id: string;
  actor: string;
  operator_message: string | null;
  error: string | null;
  progress: ProvisionEvent[];
}

export interface ProvisionStartRequest {
  actor: string;
  message?: string;
  dry_run?: boolean;
}

export interface TeardownResult {
  topology_id: number;
  qms_torn_down: number;
  details: unknown[];
}

// ────────── Fetch helpers ──────────

async function bclGet<T>(path: string): Promise<T> {
  const res = await fetch(`${BCL_BASE}${path}`, {
    headers: { "Content-Type": "application/json" },
  });
  if (!res.ok) {
    throw new Error(`BCL ${res.status}: ${await res.text()}`);
  }
  return (await res.json()) as T;
}

async function bclPost<TReq, TRes>(
  path: string,
  body: TReq,
): Promise<TRes> {
  const res = await fetch(`${BCL_BASE}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    throw new Error(`BCL ${res.status}: ${await res.text()}`);
  }
  return (await res.json()) as TRes;
}

async function bclDelete<T>(path: string): Promise<T> {
  const res = await fetch(`${BCL_BASE}${path}`, {
    method: "DELETE",
    headers: { "Content-Type": "application/json" },
  });
  if (!res.ok) {
    throw new Error(`BCL ${res.status}: ${await res.text()}`);
  }
  return (await res.json()) as T;
}

// ────────── API surface ──────────

export const bcl = {
  health: () => bclGet<Health>("/health/ready"),
  topologies: {
    list: () => bclGet<Topology[]>("/topologies"),
    get: (id: number | string) => bclGet<Topology>(`/topologies/${id}`),
  },
  audit: {
    list: (limit = 50, includeTotal = true) =>
      bclGet<AuditPage>(
        `/audit?limit=${limit}&include_total=${includeTotal}`,
      ),
    listByCorrelation: (correlationId: string) =>
      bclGet<AuditPage>(
        `/audit?correlation_id=${correlationId}&limit=200&include_total=true`,
      ),
  },
  provisioning: {
    start: (topologyId: number | string, req: ProvisionStartRequest) =>
      bclPost<ProvisionStartRequest, ProvisionRun>(
        `/topologies/${topologyId}/provision`,
        req,
      ),
    status: (topologyId: number | string, runId: string) =>
      bclGet<ProvisionRun>(
        `/topologies/${topologyId}/provision/${runId}/status`,
      ),
    listRuns: (topologyId: number | string) =>
      bclGet<ProvisionRun[]>(`/topologies/${topologyId}/provision`),
    teardown: (topologyId: number | string, actor: string) =>
      bclDelete<TeardownResult>(
        `/topologies/${topologyId}/provision?actor=${encodeURIComponent(actor)}`,
      ),
  },
};

// ────────── Display helpers ──────────

/** Compact phase label for the UI ("PERSISTENTVOLUMECLAIM_APPLY" → "PVC apply"). */
export function phaseLabel(phase: string): string {
  switch (phase) {
    case "PERSISTENTVOLUMECLAIM_APPLY":
      return "PVC apply";
    case "SECRET_APPLY":
      return "Secret apply";
    case "DEPLOYMENT_APPLY":
      return "Deployment apply";
    case "SERVICE_APPLY":
      return "Service apply";
    case "WAIT_FOR_READY":
      return "Wait for ready";
    case "COMPLETE":
      return "Complete";
    case "EXCEPTION":
      return "Exception";
    default:
      return phase.toLowerCase().replace(/_/g, " ");
  }
}

/** Wall-clock formatter that's stable across renders for SWR diffing. */
export function fmtElapsed(startIso: string, endIso: string | null): string {
  const start = Date.parse(startIso);
  const end = endIso ? Date.parse(endIso) : Date.now();
  const sec = Math.max(0, (end - start) / 1000);
  if (sec < 60) return `${sec.toFixed(1)}s`;
  const m = Math.floor(sec / 60);
  const s = Math.floor(sec % 60);
  return `${m}m ${s}s`;
}
