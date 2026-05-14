"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import useSWR from "swr";
import Link from "next/link";
import {
  bcl,
  fmtElapsed,
  phaseLabel,
  realizeCommandLabel,
  type Application,
  type MqRealizeProgressEvent,
  type MqRealizeRun,
  type ProvisionEvent,
  type ProvisionRun,
  type QueueManager,
  type TestMessageResult,
  type Topology,
} from "@/lib/bcl-client";

export default function TopologyDetail({
  params,
}: {
  params: { id: string };
}) {
  const topologyId = params.id;
  const router = useRouter();

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
  const { data: runs, mutate: mutateRuns } = useSWR<ProvisionRun[]>(
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

  // Live status polling for the currently-tracked provision run.
  const isLive =
    latestRun !== null &&
    (latestRun.state === "PENDING" || latestRun.state === "RUNNING");

  const { data: liveRun, mutate: mutateLiveRun } = useSWR<ProvisionRun>(
    isLive
      ? `/topologies/${topologyId}/provision/${latestRun!.run_id}/status`
      : null,
    () => bcl.provisioning.status(topologyId, latestRun!.run_id),
    { refreshInterval: 2000 },
  );

  const displayRun: ProvisionRun | null = liveRun ?? latestRun;

  // Audit log scoped to the most recent provision run's correlation_id.
  const correlationId = displayRun?.correlation_id;
  const { data: auditPage } = useSWR(
    correlationId ? `/audit?cid=${correlationId}` : null,
    () => bcl.audit.listByCorrelation(correlationId!),
    { refreshInterval: 3000 },
  );

  // ─── realize-mq-objects data ───────────────────────────────
  const { data: realizeRuns, mutate: mutateRealizeRuns } = useSWR<
    MqRealizeRun[]
  >(
    `/topologies/${topologyId}/realize-mq-objects`,
    () => bcl.realize.listRuns(topologyId),
    { refreshInterval: 5000 },
  );

  const latestRealize = useMemo<MqRealizeRun | null>(() => {
    if (!realizeRuns || realizeRuns.length === 0) return null;
    return [...realizeRuns].sort((a, b) =>
      a.started_at < b.started_at ? 1 : -1,
    )[0];
  }, [realizeRuns]);

  const isRealizeLive =
    latestRealize !== null &&
    (latestRealize.state === "PENDING" || latestRealize.state === "RUNNING");

  const { data: liveRealize } = useSWR<MqRealizeRun>(
    isRealizeLive
      ? `/topologies/${topologyId}/realize-mq-objects/${latestRealize!.run_id}/status`
      : null,
    () => bcl.realize.status(topologyId, latestRealize!.run_id),
    { refreshInterval: 2000 },
  );

  const displayRealize: MqRealizeRun | null = liveRealize ?? latestRealize;

  // Applications for the test-message form.
  const { data: applications } = useSWR<Application[]>(
    `/topologies/${topologyId}/applications`,
    () => bcl.topologies.listApps(topologyId),
    { refreshInterval: 30_000 },
  );

  // ─── actions ───────────────────────────────────────────────
  const [pending, setPending] = useState<
    | "provision"
    | "dry-run"
    | "teardown-pods"
    | "realize-apply"
    | "realize-teardown"
    | "test-message"
    | "delete-topology"
    | null
  >(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [confirmTeardownPods, setConfirmTeardownPods] = useState(false);
  const [confirmRealizeTeardown, setConfirmRealizeTeardown] = useState(false);
  const [confirmDeleteTopology, setConfirmDeleteTopology] = useState(false);

  // After a provision run completes, force one final refresh.
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

  // Same for realize.
  const lastObservedRealizeState = useRef<string | null>(null);
  useEffect(() => {
    if (!displayRealize) return;
    if (
      lastObservedRealizeState.current === "RUNNING" &&
      ["COMPLETED", "FAILED", "PARTIALLY_COMPLETED"].includes(
        displayRealize.state,
      )
    ) {
      mutateRealizeRuns();
    }
    lastObservedRealizeState.current = displayRealize.state;
  }, [displayRealize?.state, mutateRealizeRuns, displayRealize]);

  async function doProvisionStart(dryRun: boolean) {
    setActionError(null);
    setPending(dryRun ? "dry-run" : "provision");
    try {
      await bcl.provisioning.start(topologyId, {
        actor: "operator:raitus",
        message: dryRun ? "dry run from UI" : "provision from UI",
        dry_run: dryRun,
      });
      await mutateRuns();
    } catch (err) {
      setActionError(String(err));
    } finally {
      setPending(null);
    }
  }

  async function doProvisionTeardown() {
    setActionError(null);
    setPending("teardown-pods");
    try {
      await bcl.provisioning.teardown(topologyId, "operator:raitus");
      await Promise.all([mutateRuns(), mutateLiveRun()]);
      setConfirmTeardownPods(false);
    } catch (err) {
      setActionError(String(err));
    } finally {
      setPending(null);
    }
  }

  async function doRealizeApply() {
    setActionError(null);
    setPending("realize-apply");
    try {
      await bcl.realize.start(topologyId, {
        actor: "operator:raitus",
        message: "realize MQ objects from UI",
      });
      await mutateRealizeRuns();
    } catch (err) {
      setActionError(String(err));
    } finally {
      setPending(null);
    }
  }

  async function doRealizeTeardown() {
    setActionError(null);
    setPending("realize-teardown");
    try {
      await bcl.realize.teardown(topologyId, {
        actor: "operator:raitus",
        message: "teardown MQ objects from UI",
      });
      await mutateRealizeRuns();
      setConfirmRealizeTeardown(false);
    } catch (err) {
      setActionError(String(err));
    } finally {
      setPending(null);
    }
  }

  async function doDeleteTopology(cascade: boolean) {
    setActionError(null);
    setPending("delete-topology");
    try {
      await bcl.topologies.delete(topologyId, cascade, "operator:raitus");
      router.push("/");
    } catch (err) {
      setActionError(String(err));
      setPending(null);
    }
  }

  // ─── button state machine ──────────────────────────────────
  const provisioned =
    displayRun?.state === "COMPLETED" && (displayRun?.qms_ready ?? 0) > 0;
  const canStartProvision = !isLive && pending === null;
  const canTeardownPods = provisioned && !isLive && pending === null;

  const realized =
    displayRealize?.direction === "APPLY" &&
    displayRealize?.state === "COMPLETED";
  const canRealizeApply =
    provisioned && !isRealizeLive && pending === null;
  const canRealizeTeardown =
    realized && !isRealizeLive && pending === null;
  const canTestMessage = realized && pending === null;

  // ─── render ────────────────────────────────────────────────
  return (
    <main className="mx-auto max-w-6xl px-6 py-8">
      {/* Header */}
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
                  {topology.queue_managers.length === 1 ? "" : "s"} · created{" "}
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

      {/* Not found */}
      {topologyError && !topologyLoading && !topology && (
        <div className="panel p-8 text-center">
          <p className="text-sm text-fg-muted">
            Couldn&apos;t load topology #{topologyId}.
          </p>
          <p className="mt-1 font-mono text-xs text-fg-subtle">
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
                  <QmRow
                    key={qm.id}
                    qm={qm}
                    liveRun={displayRun}
                    liveRealize={displayRealize}
                  />
                ))}
              </div>
            )}
          </div>
        </section>
      )}

      {/* Step 1: Provisioning */}
      {topology && (
        <section className="mb-6">
          <div className="panel">
            <div className="flex items-center justify-between border-b border-border-subtle px-4 py-3">
              <div>
                <h2 className="text-sm font-medium">
                  Step 1 · Provision QM pods
                </h2>
                <p className="mt-0.5 text-xs text-fg-muted">
                  Deploy one pod per queue manager. Idempotent.
                </p>
              </div>
              {isLive && (
                <span className="pill text-accent">
                  <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-accent" />
                  live · 2s
                </span>
              )}
            </div>

            <div className="border-b border-border-subtle px-4 py-3">
              <div className="flex flex-wrap items-center gap-2">
                <button
                  type="button"
                  disabled={!canStartProvision}
                  onClick={() => doProvisionStart(false)}
                  className={btn(
                    "accent",
                    !canStartProvision || pending === "provision",
                  )}
                >
                  {pending === "provision" ? "Starting…" : "Provision"}
                </button>
                <button
                  type="button"
                  disabled={!canStartProvision}
                  onClick={() => doProvisionStart(true)}
                  className={btn(
                    "ghost",
                    !canStartProvision || pending === "dry-run",
                  )}
                >
                  {pending === "dry-run" ? "Starting…" : "Dry run"}
                </button>
                <button
                  type="button"
                  disabled={!canTeardownPods}
                  onClick={() => setConfirmTeardownPods(true)}
                  className={btn("danger", !canTeardownPods)}
                >
                  Tear down pods
                </button>
              </div>
            </div>

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

      {/* Step 2: Realize MQ objects */}
      {topology && (
        <section className="mb-6">
          <div className="panel">
            <div className="flex items-center justify-between border-b border-border-subtle px-4 py-3">
              <div>
                <h2 className="text-sm font-medium">
                  Step 2 · Realize MQ objects
                </h2>
                <p className="mt-0.5 text-xs text-fg-muted">
                  Inside each pod: create queues, channels, XMITQs from CSV.
                  Idempotent (existing objects skip-not-fail).
                </p>
              </div>
              {isRealizeLive && (
                <span className="pill text-accent">
                  <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-accent" />
                  live · 2s
                </span>
              )}
            </div>

            <div className="border-b border-border-subtle px-4 py-3">
              <div className="flex flex-wrap items-center gap-2">
                <button
                  type="button"
                  disabled={!canRealizeApply}
                  onClick={doRealizeApply}
                  className={btn("accent", !canRealizeApply)}
                >
                  {pending === "realize-apply"
                    ? "Starting…"
                    : "Realize MQ objects"}
                </button>
                <button
                  type="button"
                  disabled={!canRealizeTeardown}
                  onClick={() => setConfirmRealizeTeardown(true)}
                  className={btn("danger", !canRealizeTeardown)}
                >
                  Tear down MQ objects
                </button>
                {!provisioned && (
                  <span className="text-xs text-fg-subtle">
                    Provision pods first.
                  </span>
                )}
              </div>
            </div>

            {displayRealize ? (
              <RealizeRunPanel run={displayRealize} />
            ) : (
              <div className="px-4 py-12 text-center">
                <p className="text-sm text-fg-muted">
                  No realize runs yet.
                </p>
                <p className="mt-1 text-xs text-fg-subtle">
                  Pods must be ready before MQ objects can be realized inside
                  them.
                </p>
              </div>
            )}
          </div>
        </section>
      )}

      {/* Step 3: Test message flow */}
      {topology && (
        <section className="mb-6">
          <TestMessageCard
            topologyId={topologyId}
            applications={applications ?? []}
            enabled={canTestMessage}
            disabledReason={
              !realized ? "Realize MQ objects first." : undefined
            }
            pending={pending === "test-message"}
            onPendingChange={(p) => setPending(p ? "test-message" : null)}
            onError={setActionError}
          />
        </section>
      )}

      {/* Step 4: Migrate apps */}
      {topology && (
        <section className="mb-6">
          <div className="panel">
            <div className="flex items-center justify-between border-b border-border-subtle px-4 py-3">
              <div>
                <h2 className="text-sm font-medium">
                  Step 4 · Migrate applications
                </h2>
                <p className="mt-0.5 text-xs text-fg-muted">
                  Per-app source → target migration with state machine,
                  Little&apos;s Law drain prediction, and per-app rollback.
                </p>
              </div>
            </div>
            <div className="px-4 py-4">
              <p className="mb-3 text-xs text-fg-muted">
                Migrations are cross-topology: an app moves from a SOURCE
                topology to its dedicated target QM in a TARGET topology.
                Open the workspace to start one.
              </p>
              <Link
                href="/migrations"
                className={btn("accent", false) + " inline-block no-underline"}
              >
                Open migration workspace →
              </Link>
            </div>
          </div>
        </section>
      )}

      {/* Audit trail scoped to the most recent provision run */}
      {displayRun && auditPage && auditPage.entries.length > 0 && (
        <section className="mb-6">
          <div className="panel">
            <div className="flex items-center justify-between border-b border-border-subtle px-4 py-3">
              <div>
                <h2 className="text-sm font-medium">
                  Audit trail{" "}
                  <span className="ml-1 text-xs font-normal text-fg-muted">
                    ({auditPage.entries.length} entries · Lamport-ordered ·
                    correlation_id of most recent provision run)
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

      {/* Danger zone */}
      {topology && (
        <section className="mb-6">
          <div className="panel border-danger/30">
            <div className="border-b border-danger/20 px-4 py-3">
              <h2 className="text-sm font-medium text-danger">Danger zone</h2>
              <p className="mt-0.5 text-xs text-fg-muted">
                Cascade delete: removes MQ objects, then pods, then the
                topology row. Audit-logged.
              </p>
            </div>
            <div className="flex items-center justify-between px-4 py-3">
              <p className="text-xs text-fg-muted">
                Tear down individual layers above first if you want a partial
                operation.
              </p>
              <button
                type="button"
                onClick={() => setConfirmDeleteTopology(true)}
                disabled={pending !== null}
                className={btn("danger", pending !== null)}
              >
                Delete topology (cascade)
              </button>
            </div>
          </div>
        </section>
      )}

      {/* Action error banner */}
      {actionError && (
        <div className="fixed bottom-4 right-4 z-50 max-w-md rounded-md border border-danger/40 bg-danger/10 px-3 py-2 text-xs">
          <div className="flex items-center justify-between gap-3">
            <span className="font-medium text-danger">action failed</span>
            <button
              onClick={() => setActionError(null)}
              className="text-fg-subtle hover:text-fg"
            >
              ×
            </button>
          </div>
          <p className="mt-1 font-mono text-fg-muted">{actionError}</p>
        </div>
      )}

      {/* Modals */}
      {confirmTeardownPods && (
        <ConfirmModal
          title="Tear down pods"
          body={
            <>
              This deletes all queue manager pods, services, secrets, and
              persistent volumes for{" "}
              <span className="font-medium text-fg">{topology?.name}</span>.
              The topology row stays. Audit-logged.
            </>
          }
          danger
          pending={pending === "teardown-pods"}
          confirmLabel={
            pending === "teardown-pods" ? "Tearing down…" : "Tear down pods"
          }
          onCancel={() => setConfirmTeardownPods(false)}
          onConfirm={doProvisionTeardown}
        />
      )}

      {confirmRealizeTeardown && (
        <ConfirmModal
          title="Tear down MQ objects"
          body={
            <>
              This removes all queues, channels, and transmission queues
              created inside the pods for{" "}
              <span className="font-medium text-fg">{topology?.name}</span>.
              Pods stay running. Audit-logged.
            </>
          }
          danger
          pending={pending === "realize-teardown"}
          confirmLabel={
            pending === "realize-teardown"
              ? "Tearing down…"
              : "Tear down MQ objects"
          }
          onCancel={() => setConfirmRealizeTeardown(false)}
          onConfirm={doRealizeTeardown}
        />
      )}

      {confirmDeleteTopology && (
        <ConfirmModal
          title="Delete topology (cascade)"
          body={
            <>
              This will sequentially delete MQ objects → pods → DB row for{" "}
              <span className="font-medium text-fg">{topology?.name}</span>.
              Cannot be undone. Audit-logged at every step.
            </>
          }
          danger
          pending={pending === "delete-topology"}
          confirmLabel={
            pending === "delete-topology" ? "Deleting…" : "Delete cascade"
          }
          onCancel={() => setConfirmDeleteTopology(false)}
          onConfirm={() => doDeleteTopology(true)}
        />
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
  liveRealize,
}: {
  qm: QueueManager;
  liveRun: ProvisionRun | null;
  liveRealize: MqRealizeRun | null;
}) {
  // Find this QM's most recent provision-progress event.
  const provisionEvent = liveRun?.progress
    .filter((p) => p.qm_name === qm.qm_name)
    .at(-1);

  // Find this QM's most recent realize-progress event.
  const realizeEvents =
    liveRealize?.progress.filter((p) => p.qm_name === qm.qm_name) ?? [];
  const realizeEvent = realizeEvents.at(-1);
  const realizeApplied = realizeEvents.filter(
    (e) => e.status === "APPLIED" || e.status === "SKIPPED_IDEMPOTENT",
  ).length;
  const realizeTotal = realizeEvent?.commands_total_for_qm ?? 0;

  const status = resolveQmStatus(qm, provisionEvent, liveRun?.state);

  return (
    <div className="grid grid-cols-12 items-center gap-3 px-4 py-3 text-xs">
      <span className="col-span-1 flex justify-center">
        <span className={`h-2.5 w-2.5 rounded-full ${status.dot}`} />
      </span>
      <span className="col-span-3 font-mono text-fg">{qm.qm_name}</span>
      <span className="col-span-2">
        <span className={`pill ${status.text}`}>{status.label}</span>
      </span>
      <span className="col-span-3 truncate font-mono text-fg-subtle">
        {qm.pod_name ?? "—"}
      </span>
      <span className="col-span-2 text-right font-mono text-fg-muted">
        {realizeTotal > 0
          ? `${realizeApplied}/${realizeTotal} MQSC`
          : "—"}
      </span>
      <span className="col-span-1 text-right font-mono text-fg-muted">
        {qm.listener_port}
      </span>
    </div>
  );
}

function RunPanel({ run }: { run: ProvisionRun }) {
  const elapsed = fmtElapsed(run.started_at, run.finished_at);
  return (
    <div className="px-4 py-3">
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

function RealizeRunPanel({ run }: { run: MqRealizeRun }) {
  const elapsed = fmtElapsed(run.started_at, run.finished_at);
  return (
    <div className="px-4 py-3">
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
        <span className="col-span-3 truncate font-mono text-fg-subtle">
          {run.direction.toLowerCase()} · {run.run_id.slice(0, 8)}…
        </span>
        <span className="col-span-3 font-mono text-fg-muted">
          {run.commands_applied} applied · {run.commands_skipped_idempotent}{" "}
          skipped · {run.commands_failed} failed
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

      {run.progress.length > 0 ? (
        <div className="mt-4 overflow-hidden rounded-md border border-border-subtle">
          {run.progress.slice(-30).map((p, idx) => (
            <RealizeProgressRow key={idx} event={p} />
          ))}
          {run.progress.length > 30 && (
            <div className="border-t border-border-subtle bg-bg-subtle px-3 py-1.5 text-center text-xs text-fg-subtle">
              … {run.progress.length - 30} earlier events not shown
            </div>
          )}
        </div>
      ) : (
        <p className="mt-4 text-xs text-fg-subtle">
          Waiting for first command…
        </p>
      )}
    </div>
  );
}

function ProgressRow({ event }: { event: ProvisionEvent }) {
  const isDryRun = event.status === "DRY_RUN";
  const isDone =
    event.status === "APPLIED" || event.status === "READY" || isDryRun;
  const isFail =
    event.status === "FAILED" || event.status === "TIMEOUT";
  const isInflight =
    event.status === "APPLYING" || event.status === "WAITING";

  const icon = isFail ? "✗" : isDone ? "✓" : isInflight ? "·" : "·";
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

function RealizeProgressRow({ event }: { event: MqRealizeProgressEvent }) {
  const status = event.status ?? "";
  const isDone =
    status === "APPLIED" || status === "SKIPPED_IDEMPOTENT";
  const isFail = status === "FAILED";
  const isInflight = status === "STARTED";

  const icon = isFail ? "✗" : isDone ? "✓" : isInflight ? "·" : "·";
  const iconClass = isFail
    ? "text-danger"
    : isDone
      ? "text-success"
      : "text-fg-subtle";

  const tail =
    event.amq_code ??
    event.error?.slice(0, 24) ??
    (event.timestamp ? event.timestamp.slice(11, 19) : "");

  return (
    <div className="grid grid-cols-12 items-center gap-3 border-t border-border-subtle px-3 py-2 text-xs first:border-t-0">
      <span className="col-span-1 text-center font-mono">
        <span className={iconClass}>{icon}</span>
      </span>
      <span className="col-span-2 truncate font-mono text-fg-muted">
        {event.qm_name ?? "—"}
      </span>
      <span className="col-span-2 text-fg-subtle">
        {realizeCommandLabel(event.command_kind)}
      </span>
      <span className="col-span-3 truncate font-mono text-fg">
        {event.command_name ?? ""}
      </span>
      <span className="col-span-2">
        <span className={`pill ${realizeStatusBadge(status)}`}>
          {status.toLowerCase()}
        </span>
      </span>
      <span className="col-span-2 truncate text-right font-mono text-fg-subtle">
        {tail}
      </span>
    </div>
  );
}

function TestMessageCard({
  topologyId,
  applications,
  enabled,
  disabledReason,
  pending,
  onPendingChange,
  onError,
}: {
  topologyId: string;
  applications: Application[];
  enabled: boolean;
  disabledReason?: string;
  pending: boolean;
  onPendingChange: (p: boolean) => void;
  onError: (e: string | null) => void;
}) {
  const [producer, setProducer] = useState("");
  const [consumer, setConsumer] = useState("");
  const [payload, setPayload] = useState("PROBE-FROM-UI");
  const [result, setResult] = useState<TestMessageResult | null>(null);

  // Default to first two apps once loaded.
  useEffect(() => {
    if (applications.length > 0 && !producer) {
      setProducer(applications[0].app_id);
    }
    if (applications.length > 1 && !consumer) {
      setConsumer(applications[1].app_id);
    }
  }, [applications, producer, consumer]);

  async function send() {
    if (!producer || !consumer) return;
    onError(null);
    onPendingChange(true);
    try {
      const r = await bcl.messageFlow.send(topologyId, {
        producer_app_id: producer,
        consumer_app_id: consumer,
        payload,
        timeout_seconds: 30,
      });
      setResult(r);
    } catch (err) {
      onError(String(err));
    } finally {
      onPendingChange(false);
    }
  }

  return (
    <div className="panel">
      <div className="flex items-center justify-between border-b border-border-subtle px-4 py-3">
        <div>
          <h2 className="text-sm font-medium">
            Step 3 · Test message flow
          </h2>
          <p className="mt-0.5 text-xs text-fg-muted">
            End-to-end PUT/GET probe. Producer writes, consumer reads, BCL
            verifies and audits.
          </p>
        </div>
      </div>

      <div className="grid gap-3 px-4 py-4 sm:grid-cols-12 sm:items-end">
        <div className="sm:col-span-4">
          <label className="mb-1 block text-xs uppercase tracking-wider text-fg-subtle">
            Producer app
          </label>
          <select
            value={producer}
            onChange={(e) => setProducer(e.target.value)}
            disabled={!enabled || applications.length === 0}
            className="block w-full rounded-md border border-border-subtle bg-bg-subtle px-2 py-1.5 text-xs text-fg disabled:opacity-50"
          >
            <option value="">— select —</option>
            {applications.map((a) => (
              <option key={a.app_id} value={a.app_id}>
                {a.app_id}
                {a.app_name ? ` · ${a.app_name}` : ""}
              </option>
            ))}
          </select>
        </div>

        <div className="sm:col-span-4">
          <label className="mb-1 block text-xs uppercase tracking-wider text-fg-subtle">
            Consumer app
          </label>
          <select
            value={consumer}
            onChange={(e) => setConsumer(e.target.value)}
            disabled={!enabled || applications.length === 0}
            className="block w-full rounded-md border border-border-subtle bg-bg-subtle px-2 py-1.5 text-xs text-fg disabled:opacity-50"
          >
            <option value="">— select —</option>
            {applications.map((a) => (
              <option key={a.app_id} value={a.app_id}>
                {a.app_id}
                {a.app_name ? ` · ${a.app_name}` : ""}
              </option>
            ))}
          </select>
        </div>

        <div className="sm:col-span-3">
          <label className="mb-1 block text-xs uppercase tracking-wider text-fg-subtle">
            Payload
          </label>
          <input
            type="text"
            value={payload}
            onChange={(e) => setPayload(e.target.value)}
            disabled={!enabled}
            placeholder="text to send"
            className="block w-full rounded-md border border-border-subtle bg-bg-subtle px-3 py-1.5 text-xs text-fg placeholder:text-fg-subtle focus:border-accent focus:outline-none disabled:opacity-50"
          />
        </div>

        <div className="sm:col-span-1">
          <button
            type="button"
            onClick={send}
            disabled={!enabled || !producer || !consumer || pending}
            className={btn(
              "accent",
              !enabled || !producer || !consumer || pending,
            )}
          >
            {pending ? "…" : "Send"}
          </button>
        </div>
      </div>

      {disabledReason && (
        <div className="border-t border-border-subtle px-4 py-2">
          <p className="text-xs text-fg-subtle">{disabledReason}</p>
        </div>
      )}

      {result && (
        <div className="border-t border-border-subtle px-4 py-3">
          <TestMessageResultPanel result={result} />
        </div>
      )}
    </div>
  );
}

function TestMessageResultPanel({ result }: { result: TestMessageResult }) {
  return (
    <div>
      <div className="grid grid-cols-12 items-center gap-3 text-xs">
        <span className="col-span-2">
          <span
            className={`pill ${
              result.success ? "text-success" : "text-danger"
            }`}
          >
            <span
              className={`h-1.5 w-1.5 rounded-full ${
                result.success ? "bg-success" : "bg-danger"
              }`}
            />
            {result.success ? "delivered" : "failed"}
          </span>
        </span>
        <span className="col-span-2 font-mono text-fg">
          {result.total_duration_seconds.toFixed(2)}s
        </span>
        <span className="col-span-3 truncate font-mono text-fg-muted">
          {result.flow_kind} · {result.producer_qm}
          {result.producer_qm !== result.consumer_qm
            ? ` → ${result.consumer_qm}`
            : ""}
        </span>
        <span className="col-span-3 truncate font-mono text-fg-subtle">
          {result.producer_app_id} → {result.consumer_app_id}
        </span>
        <span className="col-span-2 text-right font-mono text-fg-subtle">
          {result.audit_lamport_first !== null
            ? `LC ${result.audit_lamport_first}–${result.audit_lamport_last}`
            : "—"}
        </span>
      </div>

      <div className="mt-3 overflow-hidden rounded-md border border-border-subtle">
        {result.steps.map((s, idx) => (
          <div
            key={idx}
            className="grid grid-cols-12 items-center gap-3 border-t border-border-subtle px-3 py-2 text-xs first:border-t-0"
          >
            <span className="col-span-1 text-center font-mono">
              <span className={s.success ? "text-success" : "text-danger"}>
                {s.success ? "✓" : "✗"}
              </span>
            </span>
            <span className="col-span-3 font-mono text-fg">{s.name}</span>
            <span className="col-span-2 font-mono text-fg-muted">
              {s.duration_seconds.toFixed(2)}s
            </span>
            <span className="col-span-5 truncate font-mono text-fg-subtle">
              {s.detail}
            </span>
            <span className="col-span-1 text-right font-mono text-fg-subtle">
              {s.audit_lamport !== null ? `LC ${s.audit_lamport}` : "—"}
            </span>
          </div>
        ))}
      </div>

      <div className="mt-3 grid grid-cols-2 gap-4 text-xs">
        <div>
          <div className="text-fg-subtle">Sent</div>
          <div className="mt-1 truncate font-mono text-fg">
            {result.payload_sent}
          </div>
        </div>
        <div>
          <div className="text-fg-subtle">
            Received{" "}
            {result.payload_matches && (
              <span className="text-success">· match</span>
            )}
            {result.payload_received && !result.payload_matches && (
              <span className="text-danger">· mismatch</span>
            )}
          </div>
          <div className="mt-1 truncate font-mono text-fg">
            {result.payload_received ?? "—"}
          </div>
        </div>
      </div>
    </div>
  );
}

function ConfirmModal({
  title,
  body,
  confirmLabel,
  danger,
  pending,
  onCancel,
  onConfirm,
}: {
  title: string;
  body: React.ReactNode;
  confirmLabel: string;
  danger?: boolean;
  pending: boolean;
  onCancel: () => void;
  onConfirm: () => void;
}) {
  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 px-4"
      role="dialog"
      aria-modal="true"
    >
      <div className="panel w-full max-w-md p-5">
        <h3 className="text-base font-semibold">{title}</h3>
        <p className="mt-2 text-sm text-fg-muted">{body}</p>
        <div className="mt-5 flex items-center justify-end gap-2">
          <button
            type="button"
            onClick={onCancel}
            className={btn("ghost", pending)}
            disabled={pending}
          >
            Cancel
          </button>
          <button
            type="button"
            onClick={onConfirm}
            className={btn(danger ? "danger" : "accent", pending)}
            disabled={pending}
          >
            {confirmLabel}
          </button>
        </div>
      </div>
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
    case "PARTIALLY_COMPLETED":
      return "text-warn";
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
    case "PARTIALLY_COMPLETED":
      return "bg-warn";
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

function realizeStatusBadge(status: string): string {
  switch (status) {
    case "APPLIED":
      return "text-success";
    case "SKIPPED_IDEMPOTENT":
      return "text-fg-muted";
    case "STARTED":
      return "text-warn";
    case "FAILED":
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
    accent: "border-accent/40 bg-accent/10 text-accent hover:bg-accent/20",
    ghost:
      "border-border-subtle bg-bg-subtle text-fg hover:bg-bg-elevated",
    danger:
      "border-danger/40 bg-danger/10 text-danger hover:bg-danger/20",
  };
  return `${base} ${variants[variant]} ${
    disabled ? "cursor-not-allowed opacity-40 hover:bg-transparent" : ""
  }`;
}
