"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import useSWR from "swr";
import Link from "next/link";
import {
  bcl,
  fmtElapsed,
  phaseLabel,
  type ProvisionEvent,
  type ProvisionRun,
  type QueueManager,
  type Topology,
} from "@/lib/bcl-client";

export default function TopologyDetail({
  params,
}: {
  params: { id: string };
}) {
  const topologyId = params.id;

  // ─── data ──────────────────────────────────────────────────
  const {
    data: topology,
    error: topologyError,
    isLoading: topologyLoading,
  } = useSWR<Topology>(
    `/topologies/${topologyId}`,
    () => bcl.topologies.get(topologyId),
    { refreshInterval: 5000 },
  );

  // List of all provision runs (most recent first by convention).
  const {
    data: runs,
    mutate: mutateRuns,
  } = useSWR<ProvisionRun[]>(
    `/topologies/${topologyId}/provision`,
    () => bcl.provisioning.listRuns(topologyId),
    { refreshInterval: 5000 },
  );

  const latestRun = useMemo<ProvisionRun | null>(() => {
    if (!runs || runs.length === 0) return null;
    return [...runs].sort((a, b) =>
      a.started_at < b.started_at ? 1 : -1,
    )[0];
  }, [runs]);

  // Live status polling for the currently-tracked run.
  // Active run = latestRun if RUNNING/PENDING; otherwise no fast-poll.
  const isLive =
    latestRun !== null &&
    (latestRun.state === "PENDING" || latestRun.state === "RUNNING");

  const {
    data: liveRun,
    mutate: mutateLiveRun,
  } = useSWR<ProvisionRun>(
    isLive ? `/topologies/${topologyId}/provision/${latestRun!.run_id}/status` : null,
    () => bcl.provisioning.status(topologyId, latestRun!.run_id),
    { refreshInterval: 2000 },
  );

  // The run we actually display (prefer live for fresh progress).
  const displayRun: ProvisionRun | null = liveRun ?? latestRun;

  // Audit log scoped to the most recent run's correlation_id.
  const correlationId = displayRun?.correlation_id;
  const { data: auditPage } = useSWR(
    correlationId ? `/audit?cid=${correlationId}` : null,
    () => bcl.audit.listByCorrelation(correlationId!),
    { refreshInterval: 3000 },
  );

  // ─── actions ───────────────────────────────────────────────
  const [pending, setPending] = useState<
    "provision" | "dry-run" | "teardown" | null
  >(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [confirmTeardown, setConfirmTeardown] = useState(false);

  // After a run completes, stop polling but force one final refresh
  // of runs + audit so the UI converges.
  const lastObservedState = useRef<string | null>(null);
  useEffect(() => {
    if (!displayRun) return;
    if (
      lastObservedState.current === "RUNNING" &&
      (displayRun.state === "COMPLETED" || displayRun.state === "FAILED")
    ) {
      mutateRuns();
    }
    lastObservedState.current = displayRun.state;
  }, [displayRun?.state, mutateRuns, displayRun]);

  async function doStart(dryRun: boolean) {
    setActionError(null);
    setPending(dryRun ? "dry-run" : "provision");
    try {
      await bcl.provisioning.start(topologyId, {
        actor: "operator:raitus",
        message: dryRun ? "dry run from UI" : "provision from UI",
        dry_run: dryRun,
      });
      // Re-fetch the runs list — SWR will pick up the new run and start
      // live-polling it via the isLive guard above.
      await mutateRuns();
    } catch (err) {
      setActionError(String(err));
    } finally {
      setPending(null);
    }
  }

  async function doTeardown() {
    setActionError(null);
    setPending("teardown");
    try {
      await bcl.provisioning.teardown(topologyId, "operator:raitus");
      // Teardown is synchronous — refresh everything.
      await Promise.all([mutateRuns(), mutateLiveRun()]);
      setConfirmTeardown(false);
    } catch (err) {
      setActionError(String(err));
    } finally {
      setPending(null);
    }
  }

  // ─── button state machine ──────────────────────────────────
  const provisioned =
    displayRun?.state === "COMPLETED" && (displayRun?.qms_ready ?? 0) > 0;
  const canStart = !isLive && pending === null;
  const canTeardown = provisioned && !isLive && pending === null;

  // ─── render ────────────────────────────────────────────────
  return (
    <main className="mx-auto max-w-6xl px-6 py-8">
      {/* Header / back link */}
      <header className="mb-8 border-b border-border-subtle pb-6">
        <Link
          href="/"
          className="mb-3 inline-flex items-center gap-1.5 text-xs text-fg-muted hover:text-fg"
        >
          <span aria-hidden>←</span>
          <span>Back to dashboard</span>
        </Link>
        <div className="flex items-baseline justify-between gap-3">
          <div>
            <div className="flex items-baseline gap-3">
              <h1 className="text-2xl font-semibold tracking-tight">
                {topology?.name ?? `Topology #${topologyId}`}
              </h1>
              {topology && (
                <span
                  className={`pill ${
                    topology.kind === "SOURCE"
                      ? "text-fg-muted"
                      : "text-accent"
                  }`}
                >
                  {topology.kind.toLowerCase()}
                </span>
              )}
            </div>
            <p className="mt-1 text-sm text-fg-muted">
              {topology ? (
                <>
                  id <span className="font-mono">#{topology.id}</span> ·{" "}
                  {topology.queue_managers.length} QM
                  {topology.queue_managers.length === 1 ? "" : "s"} ·
                  created{" "}
                  <span className="font-mono">
                    {topology.created_at.slice(0, 10)}
                  </span>
                </>
              ) : topologyLoading ? (
                "loading…"
              ) : topologyError ? (
                <span className="text-danger">
                  failed to load: {String(topologyError)}
                </span>
              ) : (
                ""
              )}
            </p>
          </div>
        </div>
      </header>

      {/* Topology not found */}
      {topologyError && !topologyLoading && !topology && (
        <div className="panel p-8 text-center">
          <p className="text-sm text-fg-muted">
            Couldn&apos;t load topology #{topologyId}.
          </p>
          <p className="mt-1 text-xs text-fg-subtle font-mono">
            {String(topologyError)}
          </p>
          <Link
            href="/"
            className="mt-4 inline-block text-xs text-accent hover:underline"
          >
            ← Back to dashboard
          </Link>
        </div>
      )}

      {/* Queue managers list */}
      {topology && (
        <section className="mb-6">
          <div className="panel">
            <div className="flex items-center justify-between border-b border-border-subtle px-4 py-3">
              <div>
                <h2 className="text-sm font-medium">Queue managers</h2>
                <p className="mt-0.5 text-xs text-fg-muted">
                  One pod per QM, secret-mounted passwords, PVC at /mnt/mqm.
                </p>
              </div>
            </div>
            {topology.queue_managers.length === 0 ? (
              <div className="px-4 py-12 text-center text-sm text-fg-muted">
                No queue managers in this topology.
              </div>
            ) : (
              <div className="divide-y divide-border-subtle">
                {topology.queue_managers.map((qm) => (
                  <QmRow key={qm.id} qm={qm} liveRun={displayRun} />
                ))}
              </div>
            )}
          </div>
        </section>
      )}

      {/* Provisioning controls */}
      {topology && (
        <section className="mb-6">
          <div className="panel">
            <div className="flex items-center justify-between border-b border-border-subtle px-4 py-3">
              <div>
                <h2 className="text-sm font-medium">Provisioning</h2>
                <p className="mt-0.5 text-xs text-fg-muted">
                  All actions logged via the BCL with Lamport timestamps.
                </p>
              </div>
              {isLive && (
                <span className="pill text-accent">
                  <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-accent" />
                  live · 2s
                </span>
              )}
            </div>

            {/* Action buttons */}
            <div className="border-b border-border-subtle px-4 py-3">
              <div className="flex flex-wrap items-center gap-2">
                <button
                  type="button"
                  disabled={!canStart}
                  onClick={() => doStart(false)}
                  className={btn(
                    "accent",
                    !canStart || pending === "provision",
                  )}
                >
                  {pending === "provision" ? "Starting…" : "Provision"}
                </button>
                <button
                  type="button"
                  disabled={!canStart}
                  onClick={() => doStart(true)}
                  className={btn("ghost", !canStart || pending === "dry-run")}
                >
                  {pending === "dry-run" ? "Starting…" : "Dry run"}
                </button>
                <button
                  type="button"
                  disabled={!canTeardown}
                  onClick={() => setConfirmTeardown(true)}
                  className={btn("danger", !canTeardown)}
                >
                  Tear down
                </button>
                {actionError && (
                  <span className="ml-auto max-w-[60%] truncate text-xs text-danger">
                    {actionError}
                  </span>
                )}
              </div>
            </div>

            {/* Current run status */}
            {displayRun ? (
              <RunPanel run={displayRun} />
            ) : (
              <div className="px-4 py-12 text-center">
                <p className="text-sm text-fg-muted">
                  No provision runs yet for this topology.
                </p>
                <p className="mt-1 text-xs text-fg-subtle">
                  Click <span className="font-medium text-fg">Dry run</span>{" "}
                  first to validate, then{" "}
                  <span className="font-medium text-fg">Provision</span>.
                </p>
              </div>
            )}
          </div>
        </section>
      )}

      {/* Audit trail scoped to this run */}
      {displayRun && auditPage && auditPage.entries.length > 0 && (
        <section className="mb-6">
          <div className="panel">
            <div className="flex items-center justify-between border-b border-border-subtle px-4 py-3">
              <div>
                <h2 className="text-sm font-medium">
                  Audit trail{" "}
                  <span className="ml-1 text-xs font-normal text-fg-muted">
                    ({auditPage.entries.length} entries · Lamport-ordered)
                  </span>
                </h2>
                <p className="mt-0.5 text-xs text-fg-muted">
                  correlation_id{" "}
                  <span className="font-mono text-fg-subtle">
                    {displayRun.correlation_id}
                  </span>
                </p>
              </div>
            </div>
            <div className="divide-y divide-border-subtle">
              {[...auditPage.entries]
                .sort((a, b) => a.lamport_clock - b.lamport_clock)
                .map((e) => (
                  <div
                    key={e.id}
                    className="grid grid-cols-12 items-center gap-3 px-4 py-2 text-xs"
                  >
                    <span
                      className={`pill col-span-2 justify-center font-mono ${
                        e.success ? "text-success" : "text-danger"
                      }`}
                    >
                      LC {e.lamport_clock}
                    </span>
                    <span className="col-span-5 truncate font-mono text-fg">
                      {e.operation}
                    </span>
                    <span className="col-span-3 truncate font-mono text-fg-muted">
                      {e.qm_name ?? "—"}
                    </span>
                    <span className="col-span-2 truncate text-right text-fg-subtle">
                      {e.actor.split(":").pop()}
                    </span>
                  </div>
                ))}
            </div>
          </div>
        </section>
      )}

      {/* Teardown confirmation modal */}
      {confirmTeardown && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 px-4"
          role="dialog"
          aria-modal="true"
        >
          <div className="panel w-full max-w-md p-5">
            <h3 className="text-base font-semibold">Confirm teardown</h3>
            <p className="mt-2 text-sm text-fg-muted">
              This deletes all queue manager pods, services, secrets, and
              persistent volumes for{" "}
              <span className="font-medium text-fg">{topology?.name}</span>.
              The action is audit-logged and cannot be undone from this UI.
            </p>
            <div className="mt-5 flex items-center justify-end gap-2">
              <button
                type="button"
                onClick={() => setConfirmTeardown(false)}
                className={btn("ghost", false)}
                disabled={pending === "teardown"}
              >
                Cancel
              </button>
              <button
                type="button"
                onClick={doTeardown}
                className={btn("danger", pending === "teardown")}
                disabled={pending === "teardown"}
              >
                {pending === "teardown" ? "Tearing down…" : "Tear down"}
              </button>
            </div>
          </div>
        </div>
      )}

      <footer className="mt-12 border-t border-border-subtle pt-4 text-center text-xs text-fg-subtle">
        BCL is the system of record. All provisioning state derives from the
        audit log.
      </footer>
    </main>
  );
}

// ──────────── components ────────────

function QmRow({
  qm,
  liveRun,
}: {
  qm: QueueManager;
  liveRun: ProvisionRun | null;
}) {
  // Find this QM's most recent progress event in the live run (if any).
  const event = liveRun?.progress
    .filter((p) => p.qm_name === qm.qm_name)
    .at(-1);

  // Resolved status: prefer the QM's persisted is_ready, then derive from
  // the latest progress event during an active run.
  const status = resolveQmStatus(qm, event, liveRun?.state);

  return (
    <div className="grid grid-cols-12 items-center gap-3 px-4 py-3 text-xs">
      <span className="col-span-1 flex justify-center">
        <span className={`h-2.5 w-2.5 rounded-full ${status.dot}`} />
      </span>
      <span className="col-span-3 font-mono text-fg">{qm.qm_name}</span>
      <span className="col-span-2">
        <span className={`pill ${status.text}`}>{status.label}</span>
      </span>
      <span className="col-span-4 truncate font-mono text-fg-subtle">
        {qm.pod_name ?? "—"}
      </span>
      <span className="col-span-2 text-right font-mono text-fg-muted">
        {qm.listener_port}/{qm.web_port}
      </span>
    </div>
  );
}

function RunPanel({ run }: { run: ProvisionRun }) {
  const elapsed = fmtElapsed(run.started_at, run.finished_at);
  return (
    <div className="px-4 py-3">
      {/* Run header */}
      <div className="grid grid-cols-12 items-center gap-3 text-xs">
        <span className="col-span-2">
          <span className={`pill ${stateColor(run.state)}`}>
            <span
              className={`h-1.5 w-1.5 rounded-full ${stateDot(run.state)} ${run.state === "RUNNING" ? "animate-pulse" : ""}`}
            />
            {run.state.toLowerCase()}
          </span>
        </span>
        <span className="col-span-2 font-mono text-fg">{elapsed}</span>
        <span className="col-span-4 truncate font-mono text-fg-subtle">
          run {run.run_id.slice(0, 8)}…
        </span>
        <span className="col-span-2 font-mono text-fg-muted">
          {run.qms_ready}/{run.qms_total} ready
        </span>
        <span className="col-span-2 text-right text-fg-subtle">
          {run.actor.split(":").pop()}
        </span>
      </div>

      {run.error && (
        <div className="mt-3 rounded-md border border-danger/40 bg-danger/10 px-3 py-2 text-xs text-danger">
          <span className="font-medium">error · </span>
          <span className="font-mono">{run.error}</span>
        </div>
      )}

      {/* Progress events */}
      {run.progress.length > 0 ? (
        <div className="mt-4 overflow-hidden rounded-md border border-border-subtle">
          {run.progress.map((p, idx) => (
            <ProgressRow key={idx} event={p} />
          ))}
        </div>
      ) : (
        <p className="mt-4 text-xs text-fg-subtle">
          Waiting for first progress event…
        </p>
      )}
    </div>
  );
}

function ProgressRow({ event }: { event: ProvisionEvent }) {
  const isDryRun = event.status === "DRY_RUN";
  const isDone =
    event.status === "APPLIED" ||
    event.status === "READY" ||
    isDryRun;
  const isFail =
    event.status === "FAILED" || event.status === "TIMEOUT";
  const isInflight =
    event.status === "APPLYING" || event.status === "WAITING";

  const icon = isFail
    ? "✗"
    : isDone
      ? "✓"
      : isInflight
        ? "·"
        : "·";

  const iconClass = isFail
    ? "text-danger"
    : isDone
      ? "text-success"
      : "text-fg-subtle";

  return (
    <div className="grid grid-cols-12 items-center gap-3 border-t border-border-subtle px-3 py-2 text-xs first:border-t-0">
      <span className="col-span-1 text-center font-mono">
        <span className={iconClass}>{icon}</span>
      </span>
      <span className="col-span-3 truncate font-mono text-fg-muted">
        {event.qm_name}
      </span>
      <span className="col-span-3 text-fg">{phaseLabel(event.phase)}</span>
      <span className="col-span-2">
        <span className={`pill ${statusBadge(event.status)}`}>
          {event.status.toLowerCase()}
        </span>
      </span>
      <span className="col-span-3 truncate text-right font-mono text-fg-subtle">
        {event.pod_name
          ? event.pod_name
          : event.error
            ? event.error.slice(0, 64) + (event.error.length > 64 ? "…" : "")
            : event.timestamp.slice(11, 19)}
      </span>
    </div>
  );
}

// ──────────── helpers ────────────

function resolveQmStatus(
  qm: QueueManager,
  event: ProvisionEvent | undefined,
  runState: string | undefined,
): { label: string; text: string; dot: string } {
  if (qm.is_ready)
    return { label: "ready", text: "text-success", dot: "bg-success" };
  if (event) {
    if (event.status === "FAILED" || event.status === "TIMEOUT") {
      return { label: "failed", text: "text-danger", dot: "bg-danger" };
    }
    if (event.status === "READY") {
      return { label: "ready", text: "text-success", dot: "bg-success" };
    }
    if (event.status === "APPLYING" || event.status === "WAITING") {
      return { label: "provisioning", text: "text-warn", dot: "bg-warn" };
    }
  }
  if (runState === "RUNNING" || runState === "PENDING") {
    return { label: "queued", text: "text-fg-muted", dot: "bg-fg-subtle" };
  }
  return { label: "planned", text: "text-fg-muted", dot: "bg-fg-subtle" };
}

function stateColor(state: string): string {
  switch (state) {
    case "COMPLETED":
      return "text-success";
    case "RUNNING":
    case "PENDING":
      return "text-accent";
    case "FAILED":
      return "text-danger";
    default:
      return "text-fg-muted";
  }
}

function stateDot(state: string): string {
  switch (state) {
    case "COMPLETED":
      return "bg-success";
    case "RUNNING":
    case "PENDING":
      return "bg-accent";
    case "FAILED":
      return "bg-danger";
    default:
      return "bg-fg-subtle";
  }
}

function statusBadge(status: string): string {
  switch (status) {
    case "APPLIED":
    case "READY":
      return "text-success";
    case "DRY_RUN":
      return "text-fg-muted";
    case "APPLYING":
    case "WAITING":
      return "text-warn";
    case "FAILED":
    case "TIMEOUT":
      return "text-danger";
    default:
      return "text-fg-muted";
  }
}

function btn(
  variant: "accent" | "ghost" | "danger",
  disabled: boolean,
): string {
  const base =
    "rounded-md border px-3 py-1.5 text-xs font-medium transition-colors";
  const variants: Record<typeof variant, string> = {
    accent:
      "border-accent/40 bg-accent/10 text-accent hover:bg-accent/20",
    ghost:
      "border-border-subtle bg-bg-subtle text-fg hover:bg-bg-elevated",
    danger:
      "border-danger/40 bg-danger/10 text-danger hover:bg-danger/20",
  };
  return `${base} ${variants[variant]} ${
    disabled ? "cursor-not-allowed opacity-40 hover:bg-transparent" : ""
  }`;
}
