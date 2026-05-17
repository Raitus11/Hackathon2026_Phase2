"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import useSWR from "swr";
import Link from "next/link";
import {
  bcl,
  FAILURE_STATES,
  fmtElapsed,
  FORWARD_STATES,
  migrationStateColor,
  migrationStateDot,
  migrationStateLabel,
  migrationStateShortLabel,
  TERMINAL_STATES,
  type DrainRunSnapshot,
  type Migration,
  type MigrationAuditEntry,
  type MigrationDrainResponse,
  type MigrationPlanData,
  type MigrationRisk,
  type MigrationState,
  type MigrationStep,
} from "@/lib/bcl-client";

/**
 * Migration Detail Page.
 *
 * Renders one migration in full forensic detail:
 *  - Horizontal state-machine stepper (SVG; current state pulses).
 *  - Plan panel — narrative, ordering rationale, risks, planner source.
 *  - Drain widget — Little's Law prediction (L_0 / μ) + per-poll history.
 *  - Steps table — every MQSC command with success badge and rollback availability.
 *  - Lamport timeline — every audit entry for this migration's correlation_id.
 *  - Rollback control — confirmation modal + manual rollback POST.
 *
 * Polls /migrations/{id} every 2s while non-terminal, every 10s once
 * terminal. Audit + drain polled at 3s while live.
 */
export default function MigrationDetail({
  params,
}: {
  params: { id: string };
}) {
  const migrationId = params.id;
  const router = useRouter();

  // ─── data ──────────────────────────────────────────────────
  const { data: migration, mutate: mutateMigration } = useSWR<Migration>(
    `/migrations/${migrationId}`,
    () => bcl.migrations.get(migrationId),
    {
      refreshInterval: (data) => {
        if (!data) return 3000;
        return TERMINAL_STATES.has(data.state) ? 10000 : 2000;
      },
    },
  );

  const isLive =
    migration !== undefined && !TERMINAL_STATES.has(migration.state);

  const { data: auditResp } = useSWR(
    migration ? `/migrations/${migrationId}/audit` : null,
    () => bcl.migrations.audit(migrationId, 300),
    { refreshInterval: isLive ? 3000 : 15000 },
  );

  const { data: drainResp } = useSWR<MigrationDrainResponse>(
    migration ? `/migrations/${migrationId}/drain` : null,
    () => bcl.migrations.drain(migrationId),
    { refreshInterval: isLive ? 3000 : 15000 },
  );

  // ─── actions ───────────────────────────────────────────────
  const [confirmRollback, setConfirmRollback] = useState(false);
  const [rollingBack, setRollingBack] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [reason, setReason] = useState("demo: undo migration");

  async function doRollback() {
    if (!migration) return;
    setError(null);
    setRollingBack(true);
    try {
      await bcl.migrations.rollback(migrationId, {
        operator: "demo",
        reason,
      });
      await mutateMigration();
      setConfirmRollback(false);
    } catch (err) {
      setError(String(err));
    } finally {
      setRollingBack(false);
    }
  }

  // Force one extra refresh when a live migration reaches terminal so we
  // catch the final state without waiting for the next 10s tick.
  const lastObservedState = useRef<string | null>(null);
  useEffect(() => {
    if (!migration) return;
    if (
      lastObservedState.current &&
      !TERMINAL_STATES.has(
        lastObservedState.current as MigrationState,
      ) &&
      TERMINAL_STATES.has(migration.state)
    ) {
      mutateMigration();
    }
    lastObservedState.current = migration.state;
  }, [migration?.state, mutateMigration, migration]);

  // ─── derived ───────────────────────────────────────────────
  const planWrapper = migration?.plan;
  const plan: MigrationPlanData | null = planWrapper?.plan ?? null;
  const plannerSource = planWrapper?.planner_audit?.planner_source;
  const plannerModel = planWrapper?.planner_audit?.model;
  const plannerDurationMs = planWrapper?.planner_audit?.duration_ms;

  const elapsed = migration?.started_at
    ? fmtElapsed(migration.started_at, migration.completed_at)
    : "—";

  const canRollback =
    migration !== undefined &&
    migration.state !== "ROLLING_BACK" &&
    migration.state !== "ROLLED_BACK" &&
    migration.state !== "ROLLBACK_FAILED" &&
    !rollingBack;

  // ─── render ────────────────────────────────────────────────
  if (!migration) {
    return (
      <main className="mx-auto max-w-6xl px-6 py-8">
        <Link
          href="/migrations"
          className="mb-3 inline-flex items-center gap-1.5 text-xs text-fg-muted hover:text-fg"
        >
          <span aria-hidden>←</span>
          <span>Back to workspace</span>
        </Link>
        <div className="panel mt-6 p-8 text-center">
          <p className="text-sm text-fg-muted">
            Loading migration #{migrationId}…
          </p>
        </div>
      </main>
    );
  }

  return (
    <main className="mx-auto max-w-6xl px-6 py-8">
      {/* Header */}
      <header className="mb-6 border-b border-border-subtle pb-6">
        <Link
          href="/migrations"
          className="mb-3 inline-flex items-center gap-1.5 text-xs text-fg-muted hover:text-fg"
        >
          <span aria-hidden>←</span>
          <span>Back to workspace</span>
        </Link>
        <div className="flex items-baseline justify-between gap-3">
          <div>
            <div className="flex items-baseline gap-3">
              <h1 className="text-2xl font-semibold tracking-tight">
                Migration #{migration.id}
              </h1>
              <span className="font-mono text-sm text-fg-muted">
                {migration.app_id}
              </span>
              <span className={`pill ${migrationStateColor(migration.state)}`}>
                <span
                  className={`h-1.5 w-1.5 rounded-full ${migrationStateDot(migration.state)} ${
                    isLive ? "animate-pulse" : ""
                  }`}
                />
                {migrationStateLabel(migration.state)}
              </span>
            </div>
            <p className="mt-1 text-sm text-fg-muted">
              v{migration.version} · elapsed{" "}
              <span className="font-mono text-fg">{elapsed}</span>
              {plannerSource && (
                <>
                  {" "}
                  · plan from{" "}
                  <span
                    className={
                      plannerSource === "llm" ? "text-accent" : "text-fg-muted"
                    }
                  >
                    {plannerSource === "llm"
                      ? `LLM (${plannerModel ?? "?"})`
                      : "deterministic fallback"}
                  </span>
                  {plannerDurationMs !== undefined && (
                    <span className="font-mono text-fg-subtle">
                      {" "}
                      · {plannerDurationMs}ms
                    </span>
                  )}
                </>
              )}
            </p>
          </div>
          <div>
            <button
              type="button"
              onClick={() => setConfirmRollback(true)}
              disabled={!canRollback}
              className={btn("danger", !canRollback)}
            >
              Rollback
            </button>
          </div>
        </div>
      </header>

      {/* State machine stepper */}
      <section className="mb-6">
        <div className="panel">
          <div className="border-b border-border-subtle px-4 py-3">
            <h2 className="text-sm font-medium">State machine</h2>
            <p className="mt-0.5 text-xs text-fg-muted">
              Forward path. On any failure, the engine transitions to{" "}
              <span className="font-mono">ROLLING_BACK</span> and walks{" "}
              <span className="font-mono">MigrationStep.rollback_payload</span>{" "}
              in reverse Lamport order.
            </p>
          </div>
          <div className="px-4 py-6">
            <StateStepper current={migration.state} />
          </div>
        </div>
      </section>

      {/* Plan panel */}
      {plan && (
        <section className="mb-6">
          <PlanPanel plan={plan} />
        </section>
      )}

      {/* Drain widget */}
      {drainResp && drainResp.drain_runs.length > 0 && (
        <section className="mb-6">
          <DrainPanel drain={drainResp} />
        </section>
      )}

      {/* Steps table */}
      {migration.steps.length > 0 && (
        <section className="mb-6">
          <StepsPanel steps={migration.steps} />
        </section>
      )}

      {/* Lamport timeline */}
      {auditResp && auditResp.entries.length > 0 && (
        <section className="mb-6">
          <LamportTimeline
            entries={auditResp.entries}
            correlationId={auditResp.correlation_id}
          />
        </section>
      )}

      {/* Rollback strategy if present */}
      {plan?.rollback_strategy && (
        <section className="mb-6">
          <div className="panel">
            <div className="border-b border-border-subtle px-4 py-3">
              <h2 className="text-sm font-medium">Rollback strategy</h2>
              <p className="mt-0.5 text-xs text-fg-muted">
                What the rollback engine will do if you click{" "}
                <strong>Rollback</strong> above, or if forward-step failure
                fires it automatically.
              </p>
            </div>
            <div className="px-4 py-3">
              <p className="text-sm text-fg-muted">{plan.rollback_strategy}</p>
            </div>
          </div>
        </section>
      )}

      {/* Error banner */}
      {error && (
        <div className="fixed bottom-4 right-4 z-50 max-w-md rounded-md border border-danger/40 bg-danger/10 px-3 py-2 text-xs">
          <div className="flex items-center justify-between gap-3">
            <span className="font-medium text-danger">action failed</span>
            <button
              onClick={() => setError(null)}
              className="text-fg-subtle hover:text-fg"
            >
              ×
            </button>
          </div>
          <p className="mt-1 font-mono text-fg-muted">{error}</p>
        </div>
      )}

      {/* Rollback confirmation modal */}
      {confirmRollback && (
        <ConfirmModal
          title="Rollback migration"
          body={
            <>
              <p className="mb-3">
                This walks{" "}
                <span className="font-mono">MigrationStep</span> rows in reverse
                Lamport order and executes each one&apos;s{" "}
                <span className="font-mono">rollback_payload</span> MQSC
                against the same QM that ran the forward command. Per-app:
                only this app&apos;s MQ objects are touched.
              </p>
              <label className="mb-1 block text-xs uppercase tracking-wider text-fg-subtle">
                Reason (audit-logged)
              </label>
              <input
                type="text"
                value={reason}
                onChange={(e) => setReason(e.target.value)}
                disabled={rollingBack}
                className="block w-full rounded-md border border-border-subtle bg-bg-subtle px-3 py-1.5 text-xs text-fg disabled:opacity-50"
              />
            </>
          }
          danger
          pending={rollingBack}
          confirmLabel={rollingBack ? "Rolling back…" : "Rollback"}
          onCancel={() => setConfirmRollback(false)}
          onConfirm={doRollback}
        />
      )}

      <footer className="mt-12 border-t border-border-subtle pt-4 text-center text-xs text-fg-subtle">
        Lamport (1978): logical clocks order events by causality, not wall
        time. Little (1961): for any work-conserving queue,{" "}
        <span className="font-mono">L = λW</span>; drain time predicted from
        observed depth and consumer service rate.
      </footer>
    </main>
  );
}

// ════════════════════════════════════════════════════════════════════════
// State machine stepper — horizontal SVG
// ════════════════════════════════════════════════════════════════════════

function StateStepper({ current }: { current: MigrationState }) {
  // The 9 forward states are laid out as circles connected by lines.
  // Index of the current state determines which circles are filled.
  const isFailure = FAILURE_STATES.has(current);
  const currentIdx = FORWARD_STATES.indexOf(current);

  // If we're in a failure branch, find the last forward state we visited
  // by inspecting which forward state we just left. For the UI we'll
  // simply mark all forward circles as "done" up to whatever has actually
  // happened — but since we don't have step-by-step state history in
  // memory here, we use a conservative heuristic: in failure states,
  // show all circles dimmed except the failure pill below.
  const stateIdx = isFailure ? -1 : currentIdx;

  return (
    <div className="w-full">
      {/* Forward path stepper */}
      <div className="relative">
        {/* Connecting line */}
        <div
          className="absolute left-0 right-0 top-4 h-0.5 bg-border-subtle"
          aria-hidden
        />
        {/* Filled portion of the line */}
        {stateIdx > 0 && (
          <div
            className="absolute left-0 top-4 h-0.5 bg-accent transition-all duration-300"
            style={{
              width: `${(stateIdx / (FORWARD_STATES.length - 1)) * 100}%`,
            }}
            aria-hidden
          />
        )}

        <div className="relative grid grid-cols-9 gap-1">
          {FORWARD_STATES.map((s, i) => {
            const done = stateIdx > i || s === "COMPLETED" && current === "COMPLETED";
            const isCurrent = !isFailure && s === current;
            const isPast = stateIdx > i;
            const dotClass = done
              ? "bg-accent border-accent"
              : isCurrent
                ? "bg-accent border-accent animate-pulse"
                : "bg-bg-base border-border-subtle";
            const labelClass = isPast
              ? "text-fg-muted"
              : isCurrent
                ? "text-fg"
                : "text-fg-subtle";

            // Highlight COMPLETED if migration is COMPLETED.
            const completedHighlight =
              s === "COMPLETED" && current === "COMPLETED"
                ? "bg-success border-success"
                : "";

            return (
              <div
                key={s}
                className="flex flex-col items-center gap-1.5"
              >
                <div
                  className={`relative z-10 h-8 w-8 rounded-full border-2 ${dotClass} ${completedHighlight} transition-colors`}
                  title={s}
                >
                  {/* Optional checkmark on done states */}
                  {(isPast ||
                    (s === "COMPLETED" && current === "COMPLETED")) && (
                    <svg
                      viewBox="0 0 24 24"
                      className="absolute inset-0 m-auto h-4 w-4 text-bg-base"
                      fill="none"
                      stroke="currentColor"
                      strokeWidth="3"
                      strokeLinecap="round"
                      strokeLinejoin="round"
                      aria-hidden
                    >
                      <polyline points="20 6 9 17 4 12" />
                    </svg>
                  )}
                </div>
                <span
                  className={`text-center font-mono text-[10px] leading-tight ${labelClass}`}
                >
                  {migrationStateShortLabel(s)}
                </span>
              </div>
            );
          })}
        </div>
      </div>

      {/* Failure branch — shown only if we're in a failure state */}
      {isFailure && (
        <div className="mt-6 rounded-md border border-danger/40 bg-danger/5 p-4">
          <div className="flex items-center gap-3">
            <span className={`pill ${migrationStateColor(current)}`}>
              <span
                className={`h-1.5 w-1.5 rounded-full ${migrationStateDot(current)} ${
                  current === "ROLLING_BACK" ? "animate-pulse" : ""
                }`}
              />
              {migrationStateLabel(current)}
            </span>
            <p className="text-xs text-fg-muted">
              {current === "ROLLING_BACK"
                ? "Reverse-Lamport walk in progress. Each forward step is being inverted."
                : current === "ROLLED_BACK"
                  ? "Every forward step has been inverted. Source state restored."
                  : "Rollback could not fully invert. Human intervention required."}
            </p>
          </div>
        </div>
      )}
    </div>
  );
}

// ════════════════════════════════════════════════════════════════════════
// Plan panel
// ════════════════════════════════════════════════════════════════════════

function PlanPanel({ plan }: { plan: MigrationPlanData }) {
  const sevOrder: Record<MigrationRisk["severity"], number> = {
    CRITICAL: 0,
    HIGH: 1,
    MEDIUM: 2,
    LOW: 3,
  };
  const sortedRisks = [...plan.risks].sort(
    (a, b) => sevOrder[a.severity] - sevOrder[b.severity],
  );

  return (
    <div className="panel">
      <div className="border-b border-border-subtle px-4 py-3">
        <h2 className="text-sm font-medium">Plan</h2>
        <p className="mt-0.5 text-xs text-fg-muted">
          The Migration Planner agent&apos;s structured output. Operational
          fields (bridge name, queues) are pinned by the engine; narrative +
          risks are advisory.
        </p>
      </div>

      <div className="px-4 py-4">
        <p className="text-sm text-fg">{plan.narrative}</p>

        <div className="mt-4 grid gap-3 sm:grid-cols-2">
          <div className="rounded-md border border-border-subtle bg-bg-subtle p-3">
            <div className="text-xs uppercase tracking-wider text-fg-subtle">
              Bridge channel
            </div>
            <div className="mt-1 truncate font-mono text-xs text-fg">
              {plan.bridge_channel_name}
            </div>
            <div className="mt-1.5 text-[11px] italic text-fg-subtle">
              SDR/RCVR sender-receiver pair: bidirectional message
              commitment with per-channel FIFO preservation.
            </div>
          </div>
          <div className="rounded-md border border-border-subtle bg-bg-subtle p-3">
            <div className="text-xs uppercase tracking-wider text-fg-subtle">
              Bridge XMITQ
            </div>
            <div className="mt-1 truncate font-mono text-xs text-fg">
              {plan.bridge_xmitq_name}
            </div>
          </div>
          <div className="rounded-md border border-border-subtle bg-bg-subtle p-3">
            <div className="text-xs uppercase tracking-wider text-fg-subtle">
              Predicted duration
            </div>
            <div className="mt-1 font-mono text-xs text-fg">
              ~{plan.predicted_duration_seconds}s
            </div>
          </div>
          <div className="rounded-md border border-border-subtle bg-bg-subtle p-3">
            <div className="text-xs uppercase tracking-wider text-fg-subtle">
              Queues to redirect
            </div>
            <div className="mt-1 font-mono text-xs text-fg">
              {plan.queues_to_redirect.length}
            </div>
          </div>
        </div>

        {plan.queues_to_redirect.length > 0 && (
          <details className="mt-3">
            <summary className="cursor-pointer text-xs text-fg-muted hover:text-fg">
              Queue list ({plan.queues_to_redirect.length})
            </summary>
            <div className="mt-2 grid grid-cols-2 gap-1 sm:grid-cols-3">
              {plan.queues_to_redirect.map((q) => (
                <span
                  key={q}
                  className="truncate rounded border border-border-subtle bg-bg-subtle px-2 py-1 font-mono text-[11px] text-fg"
                  title={q}
                >
                  {q}
                </span>
              ))}
            </div>
          </details>
        )}

        <div className="mt-4 border-t border-border-subtle pt-3">
          <div className="text-xs uppercase tracking-wider text-fg-subtle">
            Ordering rationale
          </div>
          <p className="mt-1 text-sm text-fg-muted">{plan.ordering_rationale}</p>
        </div>

        {sortedRisks.length > 0 && (
          <div className="mt-4 border-t border-border-subtle pt-3">
            <div className="mb-2 text-xs uppercase tracking-wider text-fg-subtle">
              Risks ({sortedRisks.length})
            </div>
            <div className="space-y-2">
              {sortedRisks.map((r, i) => (
                <RiskCard key={i} risk={r} />
              ))}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

function RiskCard({ risk }: { risk: MigrationRisk }) {
  const color =
    risk.severity === "CRITICAL"
      ? "text-danger border-danger/40 bg-danger/5"
      : risk.severity === "HIGH"
        ? "text-warn border-warn/40 bg-warn/5"
        : risk.severity === "MEDIUM"
          ? "text-fg border-border-subtle bg-bg-subtle"
          : "text-fg-muted border-border-subtle bg-bg-subtle";

  return (
    <div className={`rounded-md border p-3 text-xs ${color}`}>
      <div className="flex items-center gap-2">
        <span className="font-mono font-medium">{risk.severity}</span>
        <span className="text-fg-subtle">·</span>
        <span className="font-mono text-fg-muted">{risk.category}</span>
      </div>
      <p className="mt-1.5 text-fg-muted">{risk.description}</p>
      <p className="mt-1.5 text-fg-subtle">
        <span className="text-fg-muted">mitigation: </span>
        {risk.mitigation}
      </p>
    </div>
  );
}

// ════════════════════════════════════════════════════════════════════════
// Drain panel — Little's Law widget + poll history
// ════════════════════════════════════════════════════════════════════════

function DrainPanel({ drain }: { drain: MigrationDrainResponse }) {
  // Take the most recent run group and flatten its drain snapshots.
  const latest = drain.drain_runs[0];
  if (!latest || latest.drains.length === 0) return null;

  return (
    <div className="panel">
      <div className="border-b border-border-subtle px-4 py-3">
        <h2 className="text-sm font-medium">Drain · Little&apos;s Law</h2>
        <p className="mt-0.5 text-xs text-fg-muted">
          Per-queue depth + consumer service rate.{" "}
          <span className="font-mono">T_drain ≈ L₀ / μ</span> · zero-window
          condition: depth=0 ∧ IPPROCS=0 ∧ OPPROCS=0 over 3 consecutive polls.
        </p>
      </div>

      <div className="divide-y divide-border-subtle">
        {latest.drains.map((d, i) => (
          <DrainQueueRow key={d.queue + i} d={d} />
        ))}
      </div>

      <div className="border-t border-border-subtle bg-bg-subtle px-4 py-2">
        <p className="text-[11px] italic text-fg-subtle">{drain.reference}</p>
      </div>
    </div>
  );
}

function DrainQueueRow({ d }: { d: DrainRunSnapshot }) {
  const mu = d.measured_mu;
  const tDrain =
    d.initial_depth === 0
      ? 0
      : mu && mu > 0
        ? d.initial_depth / mu
        : null;

  const tDrainStr =
    tDrain === null
      ? "—"
      : tDrain === 0
        ? "0s"
        : `${tDrain.toFixed(1)}s`;
  const muStr = mu !== null ? mu.toFixed(2) + " msg/s" : "—";

  return (
    <div className="px-4 py-3">
      <div className="grid grid-cols-12 items-center gap-3 text-xs">
        <span className="col-span-3 truncate font-mono text-fg">
          {d.queue}
        </span>
        <span className="col-span-2">
          <span
            className={`pill ${
              d.drained
                ? "text-success"
                : d.error_kind === "timeout"
                  ? "text-warn"
                  : "text-danger"
            }`}
          >
            <span
              className={`h-1.5 w-1.5 rounded-full ${
                d.drained
                  ? "bg-success"
                  : d.error_kind === "timeout"
                    ? "bg-warn"
                    : "bg-danger"
              }`}
            />
            {d.drained ? "drained" : d.error_kind ?? "pending"}
          </span>
        </span>
        <span className="col-span-2 font-mono text-fg-muted">
          L₀={d.initial_depth}
        </span>
        <span className="col-span-2 font-mono text-fg-muted">μ={muStr}</span>
        <span className="col-span-2 font-mono text-fg-muted">
          T_drain={tDrainStr}
        </span>
        <span className="col-span-1 text-right font-mono text-fg-subtle">
          {d.polls}p · {d.duration_seconds.toFixed(1)}s
        </span>
      </div>

      {/* Poll history sparkline */}
      {d.history.length > 0 && (
        <div className="mt-2">
          <DrainSparkline history={d.history} />
        </div>
      )}
    </div>
  );
}

function DrainSparkline({
  history,
}: {
  history: DrainRunSnapshot["history"];
}) {
  if (history.length === 0) return null;
  const W = 600;
  const H = 40;
  const padL = 30;
  const padR = 8;
  const padT = 4;
  const padB = 4;

  const maxDepth = Math.max(
    1,
    ...history.map((h) => h.depth ?? 0),
  );
  const maxT = Math.max(
    1,
    ...history.map((h) => h.t_seconds),
  );

  // Linear scales
  const x = (t: number) =>
    padL + (t / maxT) * (W - padL - padR);
  const y = (depth: number) =>
    padT + (H - padT - padB) * (1 - depth / maxDepth);

  const points = history
    .filter((h) => h.depth !== null)
    .map((h) => `${x(h.t_seconds)},${y(h.depth ?? 0)}`)
    .join(" ");

  return (
    <svg
      viewBox={`0 0 ${W} ${H}`}
      preserveAspectRatio="xMinYMid meet"
      className="h-10 w-full"
      role="img"
      aria-label="queue depth over time"
    >
      {/* Y-axis label */}
      <text
        x={padL - 4}
        y={padT + 6}
        textAnchor="end"
        className="fill-fg-subtle font-mono"
        fontSize="9"
      >
        {maxDepth}
      </text>
      <text
        x={padL - 4}
        y={H - padB}
        textAnchor="end"
        className="fill-fg-subtle font-mono"
        fontSize="9"
      >
        0
      </text>
      {/* Zero line */}
      <line
        x1={padL}
        x2={W - padR}
        y1={H - padB}
        y2={H - padB}
        className="stroke-border-subtle"
        strokeWidth="0.5"
        strokeDasharray="2 2"
      />
      {/* Depth polyline */}
      <polyline
        points={points}
        className="fill-none stroke-accent"
        strokeWidth="1.5"
      />
      {/* Sample dots */}
      {history.map((h, i) =>
        h.depth !== null ? (
          <circle
            key={i}
            cx={x(h.t_seconds)}
            cy={y(h.depth)}
            r="1.5"
            className="fill-accent"
          />
        ) : null,
      )}
    </svg>
  );
}

// ════════════════════════════════════════════════════════════════════════
// Steps table
// ════════════════════════════════════════════════════════════════════════

function StepsPanel({ steps }: { steps: MigrationStep[] }) {
  return (
    <div className="panel">
      <div className="border-b border-border-subtle px-4 py-3">
        <h2 className="text-sm font-medium">
          Migration steps{" "}
          <span className="ml-1 text-xs font-normal text-fg-muted">
            ({steps.length})
          </span>
        </h2>
        <p className="mt-0.5 text-xs text-fg-muted">
          Every MQSC command emitted by the engine. Each has a captured
          inverse (rollback_payload) so the rollback engine can walk this
          list in reverse step_index order.
        </p>
      </div>

      <div className="divide-y divide-border-subtle">
        <div className="grid grid-cols-12 items-center gap-3 px-4 py-2 text-xs uppercase tracking-wider text-fg-subtle">
          <span className="col-span-1">#</span>
          <span className="col-span-3">step</span>
          <span className="col-span-5">mqsc</span>
          <span className="col-span-1">qm</span>
          <span className="col-span-1">↶</span>
          <span className="col-span-1 text-right">ok</span>
        </div>

        {steps.map((s) => (
          <StepRow key={s.id} step={s} />
        ))}
      </div>
    </div>
  );
}

function StepRow({ step }: { step: MigrationStep }) {
  const mqsc =
    typeof step.payload?.mqsc_text === "string"
      ? (step.payload.mqsc_text as string)
      : "";
  const stepLabel =
    typeof step.payload?.step_label === "string"
      ? (step.payload.step_label as string)
      : step.audit_op;
  const targetQmPod =
    typeof step.payload?.target_qm_pod_for === "string"
      ? (step.payload.target_qm_pod_for as string)
      : "";
  const hasRollback = step.rollback_payload !== null;

  const okIcon =
    step.succeeded === true
      ? "✓"
      : step.succeeded === false
        ? "✗"
        : "·";
  const okClass =
    step.succeeded === true
      ? "text-success"
      : step.succeeded === false
        ? "text-danger"
        : "text-fg-subtle";

  return (
    <div className="grid grid-cols-12 items-start gap-3 px-4 py-2 text-xs">
      <span className="col-span-1 font-mono text-fg-muted">
        {step.step_index}
      </span>
      <span className="col-span-3 truncate text-fg" title={stepLabel}>
        {stepLabel}
      </span>
      <span
        className="col-span-5 truncate font-mono text-fg-subtle"
        title={mqsc}
      >
        {mqsc || "—"}
      </span>
      <span className="col-span-1 font-mono text-fg-muted">
        {targetQmPod || "—"}
      </span>
      <span className="col-span-1 font-mono">
        <span
          className={
            hasRollback ? "text-fg-muted" : "text-fg-subtle opacity-50"
          }
          title={
            hasRollback ? "rollback captured" : "no inverse captured"
          }
        >
          {hasRollback ? "✓" : "—"}
        </span>
      </span>
      <span className="col-span-1 text-right font-mono">
        <span className={okClass}>{okIcon}</span>
      </span>
      {step.error_message && (
        <div className="col-span-12 mt-1 rounded border border-danger/40 bg-danger/5 px-2 py-1 font-mono text-[11px] text-danger">
          {step.error_message}
        </div>
      )}
    </div>
  );
}

// ════════════════════════════════════════════════════════════════════════
// Lamport timeline
// ════════════════════════════════════════════════════════════════════════

function LamportTimeline({
  entries,
  correlationId,
}: {
  entries: MigrationAuditEntry[];
  correlationId: string | null;
}) {
  return (
    <div className="panel">
      <div className="border-b border-border-subtle px-4 py-3">
        <h2 className="text-sm font-medium">
          Lamport timeline{" "}
          <span className="ml-1 text-xs font-normal text-fg-muted">
            ({entries.length})
          </span>
        </h2>
        <p className="mt-0.5 text-xs text-fg-muted">
          Every audit-log row for this migration&apos;s correlation_id, in
          causal (Lamport) order. correlation_id{" "}
          <span className="font-mono text-fg-subtle">
            {correlationId ?? "—"}
          </span>
        </p>
      </div>

      <div className="divide-y divide-border-subtle">
        {entries.map((e) => (
          <TimelineRow key={e.id} entry={e} />
        ))}
      </div>
    </div>
  );
}

function TimelineRow({ entry }: { entry: MigrationAuditEntry }) {
  const okClass = entry.success ? "text-success" : "text-danger";
  const ts = entry.wall_clock.slice(11, 19);

  return (
    <div className="grid grid-cols-12 items-center gap-3 px-4 py-2 text-xs">
      <span
        className={`pill col-span-2 justify-center font-mono ${
          entry.is_rollback
            ? "text-warn"
            : entry.success
              ? "text-success"
              : "text-danger"
        }`}
      >
        LC {entry.lamport_clock}
      </span>
      <span className="col-span-4 truncate font-mono text-fg">
        {entry.operation}
      </span>
      <span className="col-span-2 truncate font-mono text-fg-muted">
        {entry.qm_name ?? "—"}
      </span>
      <span className="col-span-2 truncate font-mono text-fg-subtle">
        {entry.actor.split(":").pop()}
      </span>
      <span className="col-span-1 text-right font-mono text-fg-subtle">
        {ts}
      </span>
      <span className={`col-span-1 text-right font-mono ${okClass}`}>
        {entry.is_rollback ? "↶" : entry.success ? "✓" : "✗"}
      </span>
      {entry.error_message && (
        <div className="col-span-12 mt-1 rounded border border-danger/40 bg-danger/5 px-2 py-1 font-mono text-[11px] text-danger">
          {entry.error_message}
        </div>
      )}
    </div>
  );
}

// ════════════════════════════════════════════════════════════════════════
// Modal
// ════════════════════════════════════════════════════════════════════════

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
        <div className="mt-2 text-sm text-fg-muted">{body}</div>
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

// ════════════════════════════════════════════════════════════════════════
// Button factory — matches the topology page style
// ════════════════════════════════════════════════════════════════════════

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
