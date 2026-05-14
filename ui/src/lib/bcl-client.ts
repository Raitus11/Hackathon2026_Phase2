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

// ────────── MQ Object Realization ──────────

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

// ────────── Test Message Flow ──────────

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

// ────────── Applications ──────────

export interface Application {
  app_id: string;
  app_name: string | null;
  neighbourhood: string | null;
}

// ────────── CSV Ingest ──────────

export interface CsvIngestResponse extends Topology {
  // Same shape as Topology (created via the ingest endpoint).
}

// ────────── Migration (NEW — Phase 2 migration workstream) ──────────

/**
 * Migration state machine, mirrors backend MigrationState enum.
 * Forward path runs PLANNED → PROVISIONING_TARGET_QM → VALIDATING_PRE
 * → REWIRING → DRAIN_WAIT → VALIDATING_DURING → DRAINING_SOURCE
 * → VALIDATING_POST → COMPLETED.
 * Failure path is <state> → ROLLING_BACK → ROLLED_BACK | ROLLBACK_FAILED.
 */
export type MigrationState =
  | "PLANNED"
  | "PROVISIONING_TARGET_QM"
  | "VALIDATING_PRE"
  | "REWIRING"
  | "DRAIN_WAIT"
  | "VALIDATING_DURING"
  | "DRAINING_SOURCE"
  | "VALIDATING_POST"
  | "COMPLETED"
  | "ROLLING_BACK"
  | "ROLLED_BACK"
  | "ROLLBACK_FAILED";

/** Ordered linear path through forward states. Used by the stepper. */
export const FORWARD_STATES: MigrationState[] = [
  "PLANNED",
  "PROVISIONING_TARGET_QM",
  "VALIDATING_PRE",
  "REWIRING",
  "DRAIN_WAIT",
  "VALIDATING_DURING",
  "DRAINING_SOURCE",
  "VALIDATING_POST",
  "COMPLETED",
];

export const TERMINAL_STATES = new Set<MigrationState>([
  "COMPLETED",
  "ROLLED_BACK",
  "ROLLBACK_FAILED",
]);

export const FAILURE_STATES = new Set<MigrationState>([
  "ROLLING_BACK",
  "ROLLED_BACK",
  "ROLLBACK_FAILED",
]);

export interface MigrationStep {
  id: number;
  step_index: number;
  audit_op: string;
  description: string;
  payload: Record<string, unknown>;
  rollback_payload: Record<string, unknown> | null;
  started_at: string | null;
  completed_at: string | null;
  succeeded: boolean | null;
  error_message: string | null;
}

export interface MigrationRisk {
  severity: "CRITICAL" | "HIGH" | "MEDIUM" | "LOW";
  category: string;
  description: string;
  mitigation: string;
}

export interface MigrationPlanData {
  narrative: string;
  ordering_rationale: string;
  predicted_duration_seconds: number;
  bridge_channel_name: string;
  bridge_xmitq_name: string;
  queues_to_redirect: string[];
  risks: MigrationRisk[];
  rollback_strategy: string;
}

/** Wrapper persisted in Migration.plan column on the BCL. */
export interface MigrationPlanWrapper {
  plan: MigrationPlanData;
  planner_audit: {
    planner_source: "llm" | "fallback";
    model: string;
    duration_ms: number;
    agent_invocation_id?: number;
    fallback_reason?: string;
  };
  planner_input: Record<string, unknown>;
}

export interface Migration {
  id: number;
  app_id: string;
  state: MigrationState;
  plan: MigrationPlanWrapper | null;
  started_at: string | null;
  completed_at: string | null;
  version: number;
  steps: MigrationStep[];
}

export interface MigrationStartRequest {
  app_id: string;
  source_topology_name: string;
  target_topology_name: string;
}

export interface MigrationRollbackRequest {
  operator: string;
  reason: string;
}

export interface MigrationAuditEntry {
  id: number;
  lamport_clock: number;
  wall_clock: string;
  operation: string;
  actor: string;
  qm_name: string | null;
  success: boolean;
  duration_ms: number | null;
  is_rollback: boolean;
  request_payload: Record<string, unknown> | null;
  response_payload: Record<string, unknown> | null;
  error_message: string | null;
}

export interface MigrationAuditResponse {
  migration_id: number;
  correlation_id: string | null;
  count?: number;
  entries: MigrationAuditEntry[];
  note?: string;
}

export interface DrainRunSnapshot {
  queue: string;
  drained: boolean;
  initial_depth: number;
  final_depth: number | null;
  measured_mu: number | null;
  polls: number;
  duration_seconds: number;
  error_kind: string | null;
  history: Array<{
    poll: number;
    t_seconds: number;
    depth: number | null;
    ipprocs: number | null;
    opprocs: number | null;
    error_kind: string;
  }>;
}

export interface DrainRunGroup {
  started_at: string;
  completed_at: string;
  outcome: "PASS" | "WARN" | "FAIL";
  drains: DrainRunSnapshot[];
}

export interface MigrationDrainResponse {
  migration_id: number;
  state: MigrationState;
  drain_runs: DrainRunGroup[];
  note: string;
  reference: string;
}

export interface MigrationPlanResponse {
  migration_id: number;
  state?: string;
  plan?: MigrationPlanData;
  planner_audit?: MigrationPlanWrapper["planner_audit"];
  planner_input?: Record<string, unknown>;
  note?: string;
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

    listApps: (id: number | string) =>
      bclGet<Application[]>(`/topologies/${id}/applications`),

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

  realize: {
    start: (topologyId: number | string, req: MqRealizeStartRequest) =>
      bclPost<MqRealizeStartRequest, MqRealizeRun>(
        `/topologies/${topologyId}/realize-mq-objects`,
        req,
      ),
    teardown: (topologyId: number | string, req: MqRealizeStartRequest) => {
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

  messageFlow: {
    send: (topologyId: number | string, req: TestMessageRequest) =>
      bclPost<TestMessageRequest, TestMessageResult>(
        `/topologies/${topologyId}/test-message-flow`,
        req,
      ),
  },

  /**
   * NEW: Migration workstream — per-app source -> target migration.
   *
   * Lifecycle:
   *   1. start() with app_id + source/target names -> 202, returns Migration row
   *      in PLANNED. Engine kicks off background state machine.
   *   2. get() polls state. Forward path advances through 9 states until COMPLETED.
   *   3. rollback() triggers reverse-Lamport walk of MigrationStep.rollback_payload.
   *   4. audit() and drain() are scoped reads for the live UI.
   *
   * Mirrors provisioning + realize patterns: 202 + polling, audit-logged, idempotent.
   */
  migrations: {
    start: (req: MigrationStartRequest, actor = "operator:raitus") =>
      bclPost<MigrationStartRequest, Migration>(
        `/migrations?actor=${encodeURIComponent(actor)}`,
        req,
      ),
    get: (id: number | string) =>
      bclGet<Migration>(`/migrations/${id}`),
    list: (params?: { app_id?: string; target_topology_id?: number }) => {
      const qs = new URLSearchParams();
      if (params?.app_id) qs.set("app_id", params.app_id);
      if (params?.target_topology_id)
        qs.set("target_topology_id", String(params.target_topology_id));
      const q = qs.toString();
      return bclGet<Migration[]>(`/migrations${q ? `?${q}` : ""}`);
    },
    rollback: (id: number | string, req: MigrationRollbackRequest) =>
      bclPost<MigrationRollbackRequest, Migration>(
        `/migrations/${id}/rollback`,
        req,
      ),
    audit: (id: number | string, limit = 200) =>
      bclGet<MigrationAuditResponse>(
        `/migrations/${id}/audit?limit=${limit}`,
      ),
    plan: (id: number | string) =>
      bclGet<MigrationPlanResponse>(`/migrations/${id}/plan`),
    drain: (id: number | string) =>
      bclGet<MigrationDrainResponse>(`/migrations/${id}/drain`),
  },
};

// ────────── Display helpers ──────────

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

export function fmtElapsed(startIso: string, endIso: string | null): string {
  const start = Date.parse(startIso);
  const end = endIso ? Date.parse(endIso) : Date.now();
  const sec = Math.max(0, (end - start) / 1000);
  if (sec < 60) return `${sec.toFixed(1)}s`;
  const m = Math.floor(sec / 60);
  const s = Math.floor(sec % 60);
  return `${m}m ${s}s`;
}

export function realizeCommandLabel(kind: string | null | undefined): string {
  if (!kind) return "—";
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

/** Short label for a migration state. Used in the stepper + state pill. */
export function migrationStateLabel(state: MigrationState): string {
  switch (state) {
    case "PLANNED":
      return "planned";
    case "PROVISIONING_TARGET_QM":
      return "provisioning target";
    case "VALIDATING_PRE":
      return "validating (pre)";
    case "REWIRING":
      return "rewiring";
    case "DRAIN_WAIT":
      return "drain wait";
    case "VALIDATING_DURING":
      return "validating (during)";
    case "DRAINING_SOURCE":
      return "draining source";
    case "VALIDATING_POST":
      return "validating (post)";
    case "COMPLETED":
      return "completed";
    case "ROLLING_BACK":
      return "rolling back";
    case "ROLLED_BACK":
      return "rolled back";
    case "ROLLBACK_FAILED":
      return "rollback failed";
  }
}

/** Compact stepper label (fits in ~8 chars). */
export function migrationStateShortLabel(state: MigrationState): string {
  switch (state) {
    case "PLANNED":
      return "plan";
    case "PROVISIONING_TARGET_QM":
      return "prov.";
    case "VALIDATING_PRE":
      return "v.pre";
    case "REWIRING":
      return "rewire";
    case "DRAIN_WAIT":
      return "drain";
    case "VALIDATING_DURING":
      return "v.during";
    case "DRAINING_SOURCE":
      return "drain·src";
    case "VALIDATING_POST":
      return "v.post";
    case "COMPLETED":
      return "done";
    default:
      return state.toLowerCase();
  }
}

/** Map state to text-color token. */
export function migrationStateColor(state: MigrationState): string {
  if (state === "COMPLETED") return "text-success";
  if (FAILURE_STATES.has(state)) {
    return state === "ROLLED_BACK" ? "text-warn" : "text-danger";
  }
  if (TERMINAL_STATES.has(state)) return "text-fg-muted";
  return "text-accent";
}

/** Map state to background-dot token. */
export function migrationStateDot(state: MigrationState): string {
  if (state === "COMPLETED") return "bg-success";
  if (FAILURE_STATES.has(state)) {
    return state === "ROLLED_BACK" ? "bg-warn" : "bg-danger";
  }
  if (TERMINAL_STATES.has(state)) return "bg-fg-subtle";
  return "bg-accent";
}
