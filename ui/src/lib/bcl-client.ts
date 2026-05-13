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

// ────────── MQ Object Realization (NEW) ──────────

export type MqRealizeState =
  | "PENDING"
  | "RUNNING"
  | "COMPLETED"
  | "FAILED"
  | "PARTIALLY_COMPLETED";

export type MqRealizeDirection = "APPLY" | "TEARDOWN";

export interface MqRealizeProgressEvent {
  qm_name: string;
  command_index: number;
  commands_total_for_qm: number;
  command_kind: string;
  command_name: string;
  status:
    | "STARTED"
    | "APPLIED"
    | "SKIPPED_IDEMPOTENT"
    | "FAILED"
    | string;
  timestamp: string;
  amq_code?: string | null;
  detail?: string | null;
  error?: string | null;
}

export interface MqRealizeRun {
  run_id: string;
  topology_id: number;
  direction: MqRealizeDirection;
  state: MqRealizeState;
  qms_total: number;
  qms_completed: number;
  qms_failed: number;
  commands_total: number;
  commands_applied: number;
  commands_skipped_idempotent: number;
  commands_failed: number;
  started_at: string;
  finished_at: string | null;
  correlation_id: string;
  actor: string;
  operator_message: string | null;
  error: string | null;
  progress: MqRealizeProgressEvent[];
  derived_plans_summary: Record<string, unknown> | null;
}

export interface MqRealizeStartRequest {
  actor: string;
  message?: string;
  dry_run?: boolean;
}

// ────────── Test Message Flow (NEW) ──────────

export interface MessageFlowStep {
  name: string;
  started_at: string;
  duration_seconds: number;
  success: boolean;
  detail: string;
  audit_lamport: number | null;
}

export interface TestMessageRequest {
  producer_app_id: string;
  consumer_app_id: string;
  payload?: string;
  timeout_seconds?: number;
}

export interface TestMessageResult {
  correlation_id: string;
  topology_id: number;
  producer_app_id: string;
  consumer_app_id: string;
  flow_kind: string;
  producer_qm: string;
  consumer_qm: string;
  producer_queue: string;
  consumer_queue: string;
  success: boolean;
  total_duration_seconds: number;
  payload_sent: string;
  payload_received: string | null;
  payload_matches: boolean;
  steps: MessageFlowStep[];
  audit_lamport_first: number | null;
  audit_lamport_last: number | null;
}

// ────────── Applications (NEW — needed for the test-message form) ──────────

export interface Application {
  app_id: string;
  app_name: string | null;
  neighbourhood: string | null;
}

// ────────── CSV Ingest (NEW) ──────────

export interface CsvIngestResponse extends Topology {
  // Same shape as Topology (created via the ingest endpoint).
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

/**
 * Multipart POST for CSV upload.
 * The browser sets Content-Type with the multipart boundary automatically;
 * we must NOT set it ourselves or the boundary is missing.
 */
async function bclPostForm<T>(path: string, form: FormData): Promise<T> {
  const res = await fetch(`${BCL_BASE}${path}`, {
    method: "POST",
    body: form,
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

    /**
     * NEW: ingest a topology from a CSV file via multipart upload.
     * The backend reads producer/consumer flows and creates the Topology
     * + Application + QueueManager rows in one transaction.
     */
    ingestCsv: (params: {
      file: File;
      name: string;
      kind: "SOURCE" | "TARGET";
      actor?: string;
    }) => {
      const form = new FormData();
      form.append("file", params.file);
      form.append("name", params.name);
      form.append("kind", params.kind);
      if (params.actor) form.append("actor", params.actor);
      return bclPostForm<CsvIngestResponse>("/topologies/ingest-csv", form);
    },

    /**
     * NEW: list applications participating in a topology.
     * Used to populate the producer/consumer dropdowns for the
     * test-message-flow form.
     */
    listApps: (id: number | string) =>
      bclGet<Application[]>(`/topologies/${id}/applications`),

    /**
     * NEW: delete the topology row.
     * - cascade=true → triggers a TEARDOWN realize run (delete MQ objects)
     *   then deletes pods, then deletes the topology row.
     * - cascade=false → deletes only the topology row; fails if any QM
     *   is currently ready (must tear down first).
     */
    delete: (id: number | string, cascade: boolean, actor: string) => {
      const params = new URLSearchParams({
        cascade: cascade ? "true" : "false",
        actor,
      });
      return bclDelete<{ deleted: boolean; cascade_run_id?: string }>(
        `/topologies/${id}?${params.toString()}`,
      );
    },
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

  /**
   * NEW: MQ Object Realization endpoints.
   *
   * After pods are up (provisioning.COMPLETED), call realize.start(APPLY) to
   * create the queues, channels, XMITQs derived from the CSV flow spec.
   *
   * realize.start(TEARDOWN) reverses it (deletes the MQ objects); pods stay up.
   */
  realize: {
    start: (topologyId: number | string, req: MqRealizeStartRequest) =>
      bclPost<MqRealizeStartRequest, MqRealizeRun>(
        `/topologies/${topologyId}/realize-mq-objects`,
        req,
      ),
    teardown: (topologyId: number | string, req: MqRealizeStartRequest) => {
      // DELETE with body is awkward but supported by FastAPI. We use POST-style
      // wrapper here because some proxies strip DELETE bodies.
      return fetch(`${BCL_BASE}/topologies/${topologyId}/realize-mq-objects`, {
        method: "DELETE",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(req),
      }).then(async (res) => {
        if (!res.ok) {
          throw new Error(`BCL ${res.status}: ${await res.text()}`);
        }
        return (await res.json()) as MqRealizeRun;
      });
    },
    status: (topologyId: number | string, runId: string) =>
      bclGet<MqRealizeRun>(
        `/topologies/${topologyId}/realize-mq-objects/${runId}/status`,
      ),
    listRuns: (topologyId: number | string) =>
      bclGet<MqRealizeRun[]>(
        `/topologies/${topologyId}/realize-mq-objects`,
      ),
  },

  /**
   * NEW: Test message flow — end-to-end proof.
   *
   * Picks the flow between producer_app_id and consumer_app_id, runs amqsput
   * in the producer pod, polls consumer queue depth, runs amqsget in the
   * consumer pod, returns the step-by-step trace.
   */
  messageFlow: {
    send: (topologyId: number | string, req: TestMessageRequest) =>
      bclPost<TestMessageRequest, TestMessageResult>(
        `/topologies/${topologyId}/test-message-flow`,
        req,
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

/** Short label for MqRealize commands ("CHANNEL_SDR" → "SDR channel"). */
export function realizeCommandLabel(kind: string): string {
  switch (kind) {
    case "QLOCAL":
      return "local queue";
    case "QREMOTE":
      return "remote queue";
    case "QXMIT":
      return "transmission queue";
    case "CHANNEL_SDR":
      return "sender channel";
    case "CHANNEL_RCVR":
      return "receiver channel";
    case "ALTER_QMGR":
      return "alter QMGR";
    default:
      return kind.toLowerCase().replace(/_/g, " ");
  }
}
