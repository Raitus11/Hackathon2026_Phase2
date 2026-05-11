"use client";

import useSWR from "swr";
import { bcl } from "@/lib/bcl-client";

export default function Dashboard() {
  const { data: health, error: healthError } = useSWR("/health", bcl.health, {
    refreshInterval: 5000,
  });
  const { data: topologies } = useSWR("/topologies", bcl.topologies.list, {
    refreshInterval: 10000,
  });
  const { data: audit } = useSWR(
    "/audit",
    () => bcl.audit.list(50, true),
    { refreshInterval: 3000 }
  );

  // Derived stats
  const status: string = healthError
    ? "unreachable"
    : health?.status ?? "loading";

  const statusColor =
    status === "healthy"
      ? "text-success"
      : status === "degraded"
        ? "text-warn"
        : status === "loading"
          ? "text-fg-subtle"
          : "text-danger";

  const qmCount =
    topologies?.reduce((sum, t) => sum + t.queue_managers.length, 0) ?? 0;
  const auditTotal = audit?.total_count ?? audit?.entries.length ?? 0;
  const lamport = health?.lamport_clock ?? "—";

  return (
    <main className="mx-auto max-w-6xl px-6 py-8">
      {/* Header */}
      <header className="mb-8 flex items-center justify-between border-b border-border-subtle pb-6">
        <div>
          <div className="flex items-baseline gap-3">
            <h1 className="text-2xl font-semibold tracking-tight">
              IntelliAI 2.0
            </h1>
            <span className="text-xs uppercase tracking-wider text-fg-subtle">
              intelliAI2DotO · WF Hackathon 2026 — Phase 2
            </span>
          </div>
          <p className="mt-1 text-sm text-fg-muted">
            IBM MQ migration control plane — business control layer
          </p>
        </div>
        <div className="flex items-center gap-2">
          <div className="pill">
            <span className="text-fg-subtle">ns</span>
            <span className="font-mono text-fg">roco-dev</span>
          </div>
          <div className="pill">
            <span className="text-fg-subtle">LC</span>
            <span className="font-mono text-fg">{lamport}</span>
          </div>
          <div className="pill">
            <span
              className={`h-2 w-2 rounded-full ${
                status === "healthy"
                  ? "bg-success"
                  : status === "degraded"
                    ? "bg-warn"
                    : status === "loading"
                      ? "bg-fg-subtle"
                      : "bg-danger"
              }`}
            />
            <span className={`uppercase tracking-wider ${statusColor}`}>
              {status}
            </span>
          </div>
        </div>
      </header>

      {/* Stat cards */}
      <section className="mb-6 grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <StatCard
          label="Topologies"
          value={topologies?.length ?? "—"}
          sub={
            topologies && topologies.length > 0
              ? `${topologies.filter((t) => t.kind === "SOURCE").length} source · ${topologies.filter((t) => t.kind === "TARGET").length} target`
              : "none yet"
          }
        />
        <StatCard
          label="Queue managers"
          value={qmCount}
          sub={
            health && health.mq_total_count > 0
              ? `${health.mq_reachable_count}/${health.mq_total_count} reachable`
              : "none provisioned"
          }
        />
        <StatCard
          label="Audit events"
          value={auditTotal}
          sub={`Lamport clock at ${lamport}`}
        />
        <StatCard
          label="Migrations"
          value={0}
          sub="none running"
        />
      </section>

      {/* Two-column layout: audit feed + system info */}
      <section className="grid gap-4 lg:grid-cols-3">
        {/* Audit feed */}
        <div className="panel lg:col-span-2">
          <div className="flex items-center justify-between border-b border-border-subtle px-4 py-3">
            <div>
              <h2 className="text-sm font-medium">Recent audit events</h2>
              <p className="mt-0.5 text-xs text-fg-muted">
                Lamport-ordered. Every BCL state change writes one entry.
              </p>
            </div>
            <span className="pill">
              <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-accent" />
              live · 3s
            </span>
          </div>

          {audit && audit.entries.length > 0 ? (
            <div className="divide-y divide-border-subtle">
              {audit.entries.slice(0, 10).map((e) => (
                <div
                  key={e.id}
                  className="grid grid-cols-12 items-center gap-3 px-4 py-2.5 text-xs"
                >
                  <span
                    className={`pill col-span-2 justify-center font-mono ${
                      e.success ? "text-success" : "text-danger"
                    }`}
                  >
                    LC {e.lamport_clock}
                  </span>
                  <span className="col-span-4 font-mono text-fg">
                    {e.operation}
                  </span>
                  <span className="col-span-3 truncate font-mono text-fg-subtle">
                    {e.correlation_id.slice(0, 8)}…
                  </span>
                  <span className="col-span-3 truncate text-right text-fg-muted">
                    {e.actor}
                  </span>
                </div>
              ))}
            </div>
          ) : (
            <div className="px-4 py-12 text-center">
              <p className="text-sm text-fg-muted">
                No audit events yet.
              </p>
              <p className="mt-1 text-xs text-fg-subtle">
                POST a topology via{" "}
                <a
                  href="http://localhost:8080/docs"
                  target="_blank"
                  className="text-accent underline-offset-2 hover:underline"
                >
                  Swagger UI
                </a>{" "}
                to populate the log.
              </p>
            </div>
          )}
        </div>

        {/* System info */}
        <div className="panel">
          <div className="border-b border-border-subtle px-4 py-3">
            <h2 className="text-sm font-medium">System</h2>
          </div>
          <dl className="space-y-2.5 px-4 py-3 text-xs">
            <Row
              label="Status"
              value={status}
              valueClass={statusColor}
            />
            <Row
              label="DB reachable"
              value={
                health === undefined
                  ? "—"
                  : health.db_reachable
                    ? "ok"
                    : "down"
              }
              valueClass={
                health?.db_reachable === false ? "text-danger" : ""
              }
            />
            <Row
              label="K8s reachable"
              value={
                health === undefined
                  ? "—"
                  : health.k8s_reachable
                    ? "ok"
                    : "not in cluster"
              }
            />
            <Row
              label="BCL version"
              value={
                health === undefined
                  ? "—"
                  : `v${health.bcl_version}`
              }
              valueClass="font-mono"
            />
            <Row
              label="Lamport clock"
              value={lamport}
              valueClass="font-mono"
            />
            <Row
              label="MQ pods"
              value={
                health === undefined
                  ? "—"
                  : `${health.mq_reachable_count}/${health.mq_total_count}`
              }
              valueClass="font-mono"
            />
          </dl>

          <div className="border-t border-border-subtle px-4 py-3">
            <a
              href="http://localhost:8080/docs"
              target="_blank"
              className="block text-center text-xs text-accent underline-offset-2 hover:underline"
            >
              Open BCL Swagger UI →
            </a>
          </div>
        </div>
      </section>

      {/* Footer */}
      <footer className="mt-12 border-t border-border-subtle pt-4 text-center text-xs text-fg-subtle">
        BCL talks only to MQ. UI talks only to BCL. Every state change
        Lamport-clocked and audit-logged. Phase 0 foundation.
      </footer>
    </main>
  );
}

function StatCard({
  label,
  value,
  sub,
}: {
  label: string;
  value: number | string;
  sub?: string;
}) {
  return (
    <div className="panel p-4">
      <div className="text-xs uppercase tracking-wider text-fg-subtle">
        {label}
      </div>
      <div className="mt-2 text-3xl font-semibold tracking-tight">
        {value}
      </div>
      {sub && (
        <div className="mt-1 text-xs text-fg-muted">{sub}</div>
      )}
    </div>
  );
}

function Row({
  label,
  value,
  valueClass = "",
}: {
  label: string;
  value: string | number;
  valueClass?: string;
}) {
  return (
    <div className="flex items-center justify-between">
      <dt className="text-fg-subtle">{label}</dt>
      <dd className={`font-medium ${valueClass}`}>{value}</dd>
    </div>
  );
}
