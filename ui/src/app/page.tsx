"use client";

import { useRef, useState } from "react";
import { useRouter } from "next/navigation";
import useSWR from "swr";
import Link from "next/link";
import { bcl } from "@/lib/bcl-client";

export default function Dashboard() {
  const router = useRouter();
  const { data: health, error: healthError } = useSWR("/health", bcl.health, {
    refreshInterval: 5000,
  });
  const { data: topologies, mutate: mutateTopologies } = useSWR(
    "/topologies",
    bcl.topologies.list,
    { refreshInterval: 10000 },
  );
  const { data: audit } = useSWR(
    "/audit",
    () => bcl.audit.list(50, true),
    { refreshInterval: 3000 },
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
    topologies?.reduce((sum, t) => sum + t.qm_count, 0) ?? 0;
  const auditTotal = audit?.total_count ?? audit?.entries.length ?? 0;
  const lamport = health?.lamport_clock ?? "—";

  // Live migration count for the stat card.
  const { data: migrations } = useSWR(
    "/migrations",
    () => bcl.migrations.list(),
    { refreshInterval: 5000 },
  );
  const migrationsTotal = migrations?.length ?? 0;
  const migrationsLive = (migrations ?? []).filter(
    (m) =>
      m.state !== "COMPLETED" &&
      m.state !== "ROLLED_BACK" &&
      m.state !== "ROLLBACK_FAILED",
  ).length;

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
        <Link href="/migrations" className="block transition-opacity hover:opacity-80">
          <StatCard
            label="Migrations"
            value={migrationsTotal}
            sub={
              migrationsTotal === 0
                ? "none running"
                : migrationsLive > 0
                  ? `${migrationsLive} live · ${migrationsTotal - migrationsLive} done`
                  : `${migrationsTotal} complete`
            }
          />
        </Link>
      </section>

      {/* CSV ingest */}
      <section className="mb-4">
        <CsvIngestCard
          onIngested={async (topologyId) => {
            await mutateTopologies();
            router.push(`/topology/${topologyId}`);
          }}
        />
      </section>

      {/* Topologies list */}
      <section className="mb-4">
        <div className="panel">
          <div className="flex items-center justify-between border-b border-border-subtle px-4 py-3">
            <div>
              <h2 className="text-sm font-medium">Topologies</h2>
              <p className="mt-0.5 text-xs text-fg-muted">
                Click to inspect, provision, realize MQ objects, or tear down.
              </p>
            </div>
          </div>
          {topologies && topologies.length > 0 ? (
            <div className="divide-y divide-border-subtle">
              {topologies.map((t) => (
                <Link
                  key={t.id}
                  href={`/topology/${t.id}`}
                  className="grid grid-cols-12 items-center gap-3 px-4 py-3 text-xs transition-colors hover:bg-bg-subtle"
                >
                  <span className="col-span-1 font-mono text-fg-subtle">
                    #{t.id}
                  </span>
                  <span className="col-span-5 truncate font-medium text-fg">
                    {t.name}
                  </span>
                  <span className="col-span-2">
                    <span
                      className={`pill ${
                        t.kind === "SOURCE" ? "text-fg-muted" : "text-accent"
                      }`}
                    >
                      {t.kind.toLowerCase()}
                    </span>
                  </span>
                  <span className="col-span-2 font-mono text-fg-muted">
                    {t.qm_count} QM
                    {t.qm_count === 1 ? "" : "s"}
                  </span>
                  <span className="col-span-2 text-right text-fg-subtle">
                    →
                  </span>
                </Link>
              ))}
            </div>
          ) : (
            <div className="px-4 py-12 text-center">
              <p className="text-sm text-fg-muted">No topologies yet.</p>
              <p className="mt-1 text-xs text-fg-subtle">
                Upload a CSV above to ingest one.
              </p>
            </div>
          )}
        </div>
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

          <div className="flex justify-end border-b border-border-subtle px-4 py-2">
            <a
              href={bcl.exportUrls.auditCsv()}
              className="rounded-md border border-border-subtle bg-bg-subtle px-3 py-1 text-xs text-fg hover:bg-bg-elevated"
            >
              ↓ Export audit log (.csv)
            </a>
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
              <p className="text-sm text-fg-muted">No audit events yet.</p>
              <p className="mt-1 text-xs text-fg-subtle">
                Ingest a topology to populate the log.
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
            <Row label="Status" value={status} valueClass={statusColor} />
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
                health === undefined ? "—" : `v${health.bcl_version}`
              }
              valueClass="font-mono"
            />
            <Row label="Lamport clock" value={lamport} valueClass="font-mono" />
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
        Lamport-clocked and audit-logged.
      </footer>
    </main>
  );
}

// ──────────── CSV ingest card ────────────

function CsvIngestCard({
  onIngested,
}: {
  onIngested: (topologyId: number) => Promise<void>;
}) {
  const fileInput = useRef<HTMLInputElement>(null);
  const [file, setFile] = useState<File | null>(null);
  const [name, setName] = useState("");
  const [kind, setKind] = useState<"SOURCE" | "TARGET">("SOURCE");
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Auto-fill name from filename if user hasn't typed anything.
  function onFileChange(f: File | null) {
    setFile(f);
    if (f && !name) {
      const stem = f.name.replace(/\.csv$/i, "");
      setName(`${stem}-${kind.toLowerCase()}`);
    }
  }

  async function submit() {
    if (!file || !name) return;
    setError(null);
    setPending(true);
    try {
      const result = await bcl.topologies.ingestCsv({
        file,
        name,
        kind,
        actor: "operator:demo",
      });
      // Reset
      setFile(null);
      setName("");
      if (fileInput.current) fileInput.current.value = "";
      // Hand off to caller (refresh list + navigate).
      await onIngested(result.id);
    } catch (err) {
      setError(String(err));
    } finally {
      setPending(false);
    }
  }

  return (
    <div className="panel">
      <div className="flex items-center justify-between border-b border-border-subtle px-4 py-3">
        <div>
          <h2 className="text-sm font-medium">Ingest topology from CSV</h2>
          <p className="mt-0.5 text-xs text-fg-muted">
            Header columns: producer_queue_manager, producer_application_id,
            producer_queue_name, consumer_queue_manager, consumer_application_id,
            consumer_queue_name, channel_name, flow_type, …
          </p>
        </div>
      </div>

      <div className="grid gap-3 px-4 py-4 sm:grid-cols-12 sm:items-end">
        <div className="sm:col-span-5">
          <label className="mb-1 block text-xs uppercase tracking-wider text-fg-subtle">
            CSV file
          </label>
          <input
            ref={fileInput}
            type="file"
            accept=".csv,text/csv"
            onChange={(e) => onFileChange(e.target.files?.[0] ?? null)}
            disabled={pending}
            className="block w-full rounded-md border border-border-subtle bg-bg-subtle px-2 py-1.5 text-xs text-fg file:mr-3 file:rounded file:border-0 file:bg-bg-elevated file:px-3 file:py-1 file:text-xs file:text-fg-muted hover:file:bg-border-subtle disabled:opacity-50"
          />
        </div>

        <div className="sm:col-span-4">
          <label className="mb-1 block text-xs uppercase tracking-wider text-fg-subtle">
            Topology name
          </label>
          <input
            type="text"
            value={name}
            onChange={(e) => setName(e.target.value)}
            disabled={pending}
            placeholder="e.g. ngdc-source-2026q2"
            className="block w-full rounded-md border border-border-subtle bg-bg-subtle px-3 py-1.5 text-xs text-fg placeholder:text-fg-subtle focus:border-accent focus:outline-none disabled:opacity-50"
          />
        </div>

        <div className="sm:col-span-2">
          <label className="mb-1 block text-xs uppercase tracking-wider text-fg-subtle">
            Kind
          </label>
          <select
            value={kind}
            onChange={(e) => setKind(e.target.value as "SOURCE" | "TARGET")}
            disabled={pending}
            className="block w-full rounded-md border border-border-subtle bg-bg-subtle px-2 py-1.5 text-xs text-fg focus:border-accent focus:outline-none disabled:opacity-50"
          >
            <option value="SOURCE">SOURCE</option>
            <option value="TARGET">TARGET</option>
          </select>
        </div>

        <div className="sm:col-span-1">
          <button
            type="button"
            onClick={submit}
            disabled={pending || !file || !name}
            className={`block w-full rounded-md border px-3 py-1.5 text-xs font-medium transition-colors ${
              pending || !file || !name
                ? "cursor-not-allowed border-border-subtle bg-bg-subtle text-fg-subtle opacity-40"
                : "border-accent/40 bg-accent/10 text-accent hover:bg-accent/20"
            }`}
          >
            {pending ? "…" : "Ingest"}
          </button>
        </div>
      </div>

      {error && (
        <div className="border-t border-border-subtle px-4 py-2">
          <p className="truncate text-xs text-danger">{error}</p>
        </div>
      )}
    </div>
  );
}

// ──────────── small helpers ────────────

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
      <div className="mt-2 text-3xl font-semibold tracking-tight">{value}</div>
      {sub && <div className="mt-1 text-xs text-fg-muted">{sub}</div>}
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
