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

async function bclFetch<T>(path: string): Promise<T> {
  const res = await fetch(`${BCL_BASE}${path}`, {
    headers: { "Content-Type": "application/json" },
  });
  if (!res.ok) {
    throw new Error(`BCL ${res.status}: ${await res.text()}`);
  }
  return (await res.json()) as T;
}

export const bcl = {
  health: () => bclFetch<Health>("/health/ready"),
  topologies: {
    list: () => bclFetch<Topology[]>("/topologies"),
  },
  audit: {
    list: (limit = 50, includeTotal = true) =>
      bclFetch<AuditPage>(
        `/audit?limit=${limit}&include_total=${includeTotal}`
      ),
  },
};
