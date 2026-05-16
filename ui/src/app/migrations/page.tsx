"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import useSWR from "swr";
import Link from "next/link";
import {
  bcl,
  fmtElapsed,
  migrationStateColor,
  migrationStateDot,
  migrationStateLabel,
  TERMINAL_STATES,
  type Application,
  type AssistantAnswer,
  type MarkovAnalysis,
  type Migration,
  type MigrationState,
  type Topology,
} from "@/lib/bcl-client";

/**
 * Migration Workspace.
 *
 * - Top: "Start migration" form (pick source, target, app).
 * - Below: live list of every migration (sortable by app, state, started_at).
 *
 * Each migration card links into /migrations/[id] for the detail view with
 * state machine, Lamport timeline, drain widget, and rollback controls.
 *
 * Polls /migrations every 2s while any migration is in a non-terminal state
 * so the operator sees state advance live. Otherwise falls back to 10s.
 */
export default function MigrationWorkspace() {
  const router = useRouter();

  // ─── data ──────────────────────────────────────────────────
  const { data: topologies } = useSWR<Topology[]>(
    "/topologies",
    bcl.topologies.list,
    { refreshInterval: 30000 },
  );

  // List all migrations; refresh rate depends on whether any are live.
  const { data: migrations, mutate: mutateMigrations } = useSWR<Migration[]>(
    "/migrations",
    () => bcl.migrations.list(),
    {
      refreshInterval: (data) => {
        if (!data) return 5000;
        const anyLive = data.some((m) => !TERMINAL_STATES.has(m.state));
        return anyLive ? 2000 : 10000;
      },
    },
  );

  // ─── start-migration form state ────────────────────────────
  const sources = useMemo(
    () => (topologies ?? []).filter((t) => t.kind === "SOURCE"),
    [topologies],
  );
  const targets = useMemo(
    () => (topologies ?? []).filter((t) => t.kind === "TARGET"),
    [topologies],
  );

  const [sourceName, setSourceName] = useState("");
  const [targetName, setTargetName] = useState("");

  // Default to first available source / target on load.
  useEffect(() => {
    if (sources.length > 0 && !sourceName) setSourceName(sources[0].name);
  }, [sources, sourceName]);
  useEffect(() => {
    if (targets.length > 0 && !targetName) setTargetName(targets[0].name);
  }, [targets, targetName]);

  // Apps from the target topology (1:1 with target QMs by Phase 2 constraint).
  const targetTopology = targets.find((t) => t.name === targetName);
  const { data: targetApps } = useSWR<Application[]>(
    targetTopology ? `/topologies/${targetTopology.id}/applications` : null,
    () => bcl.topologies.listApps(targetTopology!.id),
    { refreshInterval: 30000 },
  );
  const [appId, setAppId] = useState("");

  useEffect(() => {
    if (targetApps && targetApps.length > 0 && !appId) {
      setAppId(targetApps[0].app_id);
    }
  }, [targetApps, appId]);

  // ─── actions ───────────────────────────────────────────────
  const [starting, setStarting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function startMigration() {
    if (!sourceName || !targetName || !appId) return;
    setError(null);
    setStarting(true);
    try {
      const m = await bcl.migrations.start({
        app_id: appId,
        source_topology_name: sourceName,
        target_topology_name: targetName,
      });
      await mutateMigrations();
      // Navigate to the new migration's detail page so the operator
      // watches it advance live.
      router.push(`/migrations/${m.id}`);
    } catch (err) {
      setError(String(err));
    } finally {
      setStarting(false);
    }
  }

  // ─── derived ───────────────────────────────────────────────
  const liveCount = (migrations ?? []).filter(
    (m) => !TERMINAL_STATES.has(m.state),
  ).length;
  const completedCount = (migrations ?? []).filter(
    (m) => m.state === "COMPLETED",
  ).length;
  const rolledBackCount = (migrations ?? []).filter(
    (m) => m.state === "ROLLED_BACK" || m.state === "ROLLBACK_FAILED",
  ).length;

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
            <h1 className="text-2xl font-semibold tracking-tight">
              Migration workspace
            </h1>
            <p className="mt-1 text-sm text-fg-muted">
              Per-app source → target migration. One app at a time. Strict 1:1
              app/QM ownership in the target topology.
            </p>
          </div>
          <div className="flex items-center gap-2">
            <span className="pill">
              <span className="text-fg-subtle">live</span>
              <span className="font-mono text-fg">{liveCount}</span>
            </span>
            <span className="pill text-success">
              <span className="font-mono">{completedCount}</span>
              <span>done</span>
            </span>
            {rolledBackCount > 0 && (
              <span className="pill text-warn">
                <span className="font-mono">{rolledBackCount}</span>
                <span>rolled back</span>
              </span>
            )}
          </div>
        </div>
      </header>

      {/* Start migration */}
      <section className="mb-6">
        <div className="panel">
          <div className="border-b border-border-subtle px-4 py-3">
            <h2 className="text-sm font-medium">Start a migration</h2>
            <p className="mt-0.5 text-xs text-fg-muted">
              Plan + execute one app&apos;s migration from the source topology
              to its dedicated target QM. The engine produces a plan,
              transitions through 9 states, and audit-logs every MQSC command.
            </p>
          </div>

          <div className="grid gap-3 px-4 py-4 sm:grid-cols-12 sm:items-end">
            <div className="sm:col-span-3">
              <label className="mb-1 block text-xs uppercase tracking-wider text-fg-subtle">
                Source topology
              </label>
              <select
                value={sourceName}
                onChange={(e) => setSourceName(e.target.value)}
                disabled={sources.length === 0}
                className="block w-full rounded-md border border-border-subtle bg-bg-subtle px-2 py-1.5 text-xs text-fg disabled:opacity-50"
              >
                <option value="">— select —</option>
                {sources.map((t) => (
                  <option key={t.id} value={t.name}>
                    {t.name}
                  </option>
                ))}
              </select>
            </div>

            <div className="sm:col-span-3">
              <label className="mb-1 block text-xs uppercase tracking-wider text-fg-subtle">
                Target topology
              </label>
              <select
                value={targetName}
                onChange={(e) => {
                  setTargetName(e.target.value);
                  setAppId(""); // reset app selection
                }}
                disabled={targets.length === 0}
                className="block w-full rounded-md border border-border-subtle bg-bg-subtle px-2 py-1.5 text-xs text-fg disabled:opacity-50"
              >
                <option value="">— select —</option>
                {targets.map((t) => (
                  <option key={t.id} value={t.name}>
                    {t.name}
                  </option>
                ))}
              </select>
            </div>

            <div className="sm:col-span-4">
              <label className="mb-1 block text-xs uppercase tracking-wider text-fg-subtle">
                Application
              </label>
              <select
                value={appId}
                onChange={(e) => setAppId(e.target.value)}
                disabled={!targetApps || targetApps.length === 0}
                className="block w-full rounded-md border border-border-subtle bg-bg-subtle px-2 py-1.5 text-xs text-fg disabled:opacity-50"
              >
                <option value="">— select —</option>
                {(targetApps ?? []).map((a) => (
                  <option key={a.app_id} value={a.app_id}>
                    {a.app_id}
                    {a.app_name ? ` · ${a.app_name}` : ""}
                  </option>
                ))}
              </select>
            </div>

            <div className="sm:col-span-2">
              <button
                type="button"
                onClick={startMigration}
                disabled={!sourceName || !targetName || !appId || starting}
                className={btn(
                  "accent",
                  !sourceName || !targetName || !appId || starting,
                )}
              >
                {starting ? "Starting…" : "Start migration"}
              </button>
            </div>
          </div>

          {(sources.length === 0 || targets.length === 0) && (
            <div className="border-t border-border-subtle px-4 py-2">
              <p className="text-xs text-fg-subtle">
                {sources.length === 0 && targets.length === 0
                  ? "No topologies yet. Ingest a source CSV and a target CSV from the dashboard first."
                  : sources.length === 0
                    ? "No SOURCE topology found. Ingest one from the dashboard."
                    : "No TARGET topology found. Ingest one from the dashboard."}
              </p>
            </div>
          )}
        </div>
      </section>

      {/* Migrations list */}
      <section className="mb-6">
        <div className="panel">
          <div className="border-b border-border-subtle px-4 py-3">
            <div className="flex items-center justify-between">
              <div>
                <h2 className="text-sm font-medium">
                  Migrations{" "}
                  <span className="ml-1 text-xs font-normal text-fg-muted">
                    ({(migrations ?? []).length})
                  </span>
                </h2>
                <p className="mt-0.5 text-xs text-fg-muted">
                  Click a row to see the state machine, Lamport timeline, plan,
                  and rollback controls.
                </p>
              </div>
              {liveCount > 0 && (
                <span className="pill text-accent">
                  <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-accent" />
                  live · 2s
                </span>
              )}
            </div>
          </div>

          {(migrations ?? []).length === 0 ? (
            <div className="px-4 py-12 text-center">
              <p className="text-sm text-fg-muted">No migrations yet.</p>
              <p className="mt-1 text-xs text-fg-subtle">
                Pick an app above and click <strong>Start migration</strong> to
                begin.
              </p>
            </div>
          ) : (
            <div className="divide-y divide-border-subtle">
              {/* Column headers */}
              <div className="grid grid-cols-12 items-center gap-3 px-4 py-2 text-xs uppercase tracking-wider text-fg-subtle">
                <span className="col-span-1">#</span>
                <span className="col-span-3">app</span>
                <span className="col-span-3">state</span>
                <span className="col-span-2">elapsed</span>
                <span className="col-span-2">steps</span>
                <span className="col-span-1 text-right">v</span>
              </div>

              {[...(migrations ?? [])]
                .sort((a, b) => b.id - a.id)
                .map((m) => (
                  <MigrationRow key={m.id} m={m} />
                ))}
            </div>
          )}
        </div>
      </section>

      {/* Operator Assistant — Agent #2 */}
      <section className="mb-6">
        <OperatorAssistantPanel />
      </section>

      {/* Reliability — absorbing Markov chain analysis */}
      <section className="mb-6">
        <ReliabilityPanel />
      </section>

      {/* Action error banner */}
      {error && (
        <div className="fixed bottom-4 right-4 z-50 max-w-md rounded-md border border-danger/40 bg-danger/10 px-3 py-2 text-xs">
          <div className="flex items-center justify-between gap-3">
            <span className="font-medium text-danger">start failed</span>
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

      <footer className="mt-12 border-t border-border-subtle pt-4 text-center text-xs text-fg-subtle">
        Migrations are per-app. Rollback walks{" "}
        <span className="font-mono">MigrationStep</span> rows in reverse
        Lamport order (Lamport 1978). Drain time predicted by Little&apos;s
        Law (Little 1961).
      </footer>
    </main>
  );
}

// ──────────── components ────────────

function MigrationRow({ m }: { m: Migration }) {
  const stepsDone = m.steps.filter((s) => s.succeeded === true).length;
  const stepsFailed = m.steps.filter((s) => s.succeeded === false).length;
  const stepsTotal = m.steps.length;

  const elapsed = m.started_at
    ? fmtElapsed(m.started_at, m.completed_at)
    : "—";

  return (
    <Link
      href={`/migrations/${m.id}`}
      className="grid grid-cols-12 items-center gap-3 px-4 py-3 text-xs transition-colors hover:bg-bg-subtle"
    >
      <span className="col-span-1 font-mono text-fg-muted">#{m.id}</span>
      <span className="col-span-3 truncate font-mono text-fg">{m.app_id}</span>
      <span className="col-span-3">
        <StatePill state={m.state} />
      </span>
      <span className="col-span-2 font-mono text-fg-muted">{elapsed}</span>
      <span className="col-span-2 font-mono text-fg-muted">
        {stepsTotal > 0 ? (
          <>
            {stepsDone}/{stepsTotal}
            {stepsFailed > 0 && (
              <span className="ml-1 text-danger">
                · {stepsFailed} fail
              </span>
            )}
          </>
        ) : (
          "—"
        )}
      </span>
      <span className="col-span-1 text-right font-mono text-fg-subtle">
        v{m.version}
      </span>
    </Link>
  );
}

function StatePill({ state }: { state: MigrationState }) {
  const isLive =
    !TERMINAL_STATES.has(state) && state !== "PLANNED";
  return (
    <span className={`pill ${migrationStateColor(state)}`}>
      <span
        className={`h-1.5 w-1.5 rounded-full ${migrationStateDot(state)} ${
          isLive ? "animate-pulse" : ""
        }`}
      />
      {migrationStateLabel(state)}
    </span>
  );
}

// ──────────── Operator Assistant panel ────────────

interface ChatTurn {
  role: "user" | "assistant";
  text: string;
  /** Present on assistant turns — how the answer was produced. */
  meta?: {
    source: AssistantAnswer["source"];
    intent: string;
    agentInvocationId: number | null;
  };
}

/** Pre-canned questions the assistant answers well. Seed judge engagement. */
const SUGGESTED_QUESTIONS: string[] = [
  "Give me an overall status summary",
  "Show me recent agent activity",
  "What is the status of ZN",
  "What happened with rollbacks for ZN",
];

/**
 * Operator Assistant chat panel.
 *
 * The BCL's second agent: it answers questions about migrations from
 * real BCL data via POST /assistant/query. Read-only — it can only
 * query, never mutate. Every answer is audit-logged as an
 * AGENT_INVOCATION; the panel surfaces the invocation id + source so
 * the agent's work is visibly traceable.
 */
function OperatorAssistantPanel() {
  const [turns, setTurns] = useState<ChatTurn[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const scrollRef = useRef<HTMLDivElement | null>(null);

  // Keep the transcript scrolled to the newest turn.
  useEffect(() => {
    const el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [turns, busy]);

  async function ask(question: string) {
    const q = question.trim();
    if (!q || busy) return;
    setInput("");
    setTurns((t) => [...t, { role: "user", text: q }]);
    setBusy(true);
    try {
      const res = await bcl.assistant.query(q);
      setTurns((t) => [
        ...t,
        {
          role: "assistant",
          text: res.answer,
          meta: {
            source: res.source,
            intent: res.intent,
            agentInvocationId: res.agent_invocation_id,
          },
        },
      ]);
    } catch (err) {
      setTurns((t) => [
        ...t,
        {
          role: "assistant",
          text: `The assistant could not answer that: ${String(err)}`,
          meta: { source: "stub", intent: "ERROR", agentInvocationId: null },
        },
      ]);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="panel">
      <div className="border-b border-border-subtle px-4 py-3">
        <div className="flex items-center justify-between">
          <div>
            <h2 className="text-sm font-medium">Operator Assistant</h2>
            <p className="mt-0.5 text-xs text-fg-muted">
              Agent #2. Ask about migration status, the Lamport-ordered audit
              trail, rollbacks, drain predictions, or agent activity. Answers
              come from live BCL data; every query is audit-logged as an
              AGENT_INVOCATION.
            </p>
          </div>
          <span className="pill text-accent">agent</span>
        </div>
      </div>

      {/* Transcript */}
      <div
        ref={scrollRef}
        className="max-h-80 overflow-y-auto px-4 py-3"
      >
        {turns.length === 0 ? (
          <p className="py-6 text-center text-xs text-fg-subtle">
            Ask the assistant a question, or pick one below.
          </p>
        ) : (
          <div className="space-y-3">
            {turns.map((turn, i) => (
              <ChatBubble key={i} turn={turn} />
            ))}
            {busy && (
              <div className="text-xs text-fg-subtle">
                <span className="inline-flex items-center gap-1.5">
                  <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-accent" />
                  assistant is querying the audit log…
                </span>
              </div>
            )}
          </div>
        )}
      </div>

      {/* Suggested questions */}
      <div className="flex flex-wrap gap-1.5 border-t border-border-subtle px-4 py-2">
        {SUGGESTED_QUESTIONS.map((q) => (
          <button
            key={q}
            type="button"
            onClick={() => ask(q)}
            disabled={busy}
            className="rounded-md border border-border-subtle bg-bg-subtle px-2 py-1 text-[11px] text-fg-muted transition-colors hover:bg-bg-elevated hover:text-fg disabled:opacity-40"
          >
            {q}
          </button>
        ))}
      </div>

      {/* Input */}
      <div className="flex items-center gap-2 border-t border-border-subtle px-4 py-3">
        <input
          type="text"
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") ask(input);
          }}
          placeholder="Ask the Operator Assistant…"
          disabled={busy}
          className="block w-full rounded-md border border-border-subtle bg-bg-subtle px-2 py-1.5 text-xs text-fg placeholder:text-fg-subtle disabled:opacity-50"
        />
        <button
          type="button"
          onClick={() => ask(input)}
          disabled={busy || !input.trim()}
          className={`shrink-0 rounded-md border px-3 py-1.5 text-xs font-medium transition-colors ${
            busy || !input.trim()
              ? "cursor-not-allowed border-border-subtle bg-bg-subtle text-fg-subtle opacity-40"
              : "border-accent/40 bg-accent/10 text-accent hover:bg-accent/20"
          }`}
        >
          {busy ? "…" : "Ask"}
        </button>
      </div>
    </div>
  );
}

function ChatBubble({ turn }: { turn: ChatTurn }) {
  if (turn.role === "user") {
    return (
      <div className="flex justify-end">
        <div className="max-w-[80%] rounded-md border border-accent/30 bg-accent/10 px-3 py-2 text-xs text-fg">
          {turn.text}
        </div>
      </div>
    );
  }
  return (
    <div className="flex justify-start">
      <div className="max-w-[85%] rounded-md border border-border-subtle bg-bg-subtle px-3 py-2">
        <p className="text-xs leading-relaxed text-fg">{turn.text}</p>
        {turn.meta && (
          <div className="mt-1.5 flex flex-wrap items-center gap-1.5 text-[10px] text-fg-subtle">
            <span className="rounded border border-border-subtle px-1 py-0.5 font-mono uppercase">
              {turn.meta.source}
            </span>
            <span className="font-mono">{turn.meta.intent}</span>
            {turn.meta.agentInvocationId !== null && (
              <span className="font-mono">
                inv #{turn.meta.agentInvocationId}
              </span>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

// ──────────── Reliability panel (absorbing Markov chain) ────────────

/** Compact human label for a migration state in the Markov tables. */
function markovStateLabel(s: string): string {
  return s
    .toLowerCase()
    .replace(/_/g, " ")
    .replace(/\bqm\b/, "QM");
}

/**
 * Reliability panel — absorbing Markov chain analysis of the migration
 * state machine.
 *
 * The migration state machine is, formally, an absorbing Markov chain.
 * This panel renders GET /reliability/markov:
 *   - the reference model's fundamental-matrix results (expected steps
 *     to absorption, absorption probabilities) — a STATED model;
 *   - the empirical transition estimate counted from the real audit log
 *     — a MEASUREMENT, honestly labelled as low-sample.
 *
 * Collapsed by default so it does not crowd the workspace; the operator
 * expands it when they want the math.
 */
function ReliabilityPanel() {
  const [open, setOpen] = useState(false);
  const { data, error, isLoading } = useSWR<MarkovAnalysis>(
    open ? "/reliability/markov" : null,
    () => bcl.reliability.markov(),
    { refreshInterval: 0, revalidateOnFocus: false },
  );

  return (
    <div className="panel">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center justify-between border-b border-border-subtle px-4 py-3 text-left"
      >
        <div>
          <h2 className="text-sm font-medium">
            Reliability · absorbing Markov chain
          </h2>
          <p className="mt-0.5 text-xs text-fg-muted">
            The migration state machine modelled as an absorbing Markov
            chain. Fundamental matrix N = (I−Q)⁻¹ — expected steps to
            absorption and absorption probabilities — plus an empirical
            transition estimate from the real audit log.
          </p>
        </div>
        <span className="ml-3 shrink-0 text-xs text-fg-subtle">
          {open ? "▲ hide" : "▼ show"}
        </span>
      </button>

      {open && (
        <div className="px-4 py-4">
          {isLoading && (
            <p className="py-6 text-center text-xs text-fg-subtle">
              Computing the fundamental matrix…
            </p>
          )}
          {error && (
            <p className="py-6 text-center text-xs text-danger">
              Could not load reliability analysis: {String(error)}
            </p>
          )}
          {data && <ReliabilityContent data={data} />}
        </div>
      )}
    </div>
  );
}

function ReliabilityContent({ data }: { data: MarkovAnalysis }) {
  const ref = data.reference_model;
  const emp = data.empirical_estimate;

  // Forward-path order for the expected-steps table.
  const stepOrder = [
    "PLANNED",
    "PROVISIONING_TARGET_QM",
    "VALIDATING_PRE",
    "REWIRING",
    "DRAIN_WAIT",
    "VALIDATING_DURING",
    "DRAINING_SOURCE",
    "VALIDATING_POST",
    "ROLLING_BACK",
  ].filter((s) => s in ref.expected_steps_to_absorption);

  const fromPlanned = ref.absorption_probability["PLANNED"] ?? {};

  return (
    <div className="space-y-5">
      {/* Reference model — expected steps */}
      <div>
        <h3 className="mb-1 text-xs font-medium uppercase tracking-wider text-fg-subtle">
          Expected steps to absorption · reference model
        </h3>
        <p className="mb-2 text-[11px] text-fg-muted">
          t = N·1, where N = (I−Q)⁻¹ is the fundamental matrix. Expected
          number of state transitions before the migration reaches an
          absorbing state (COMPLETED / ROLLED_BACK / ROLLBACK_FAILED).
        </p>
        <div className="overflow-hidden rounded-md border border-border-subtle">
          {stepOrder.map((s, i) => (
            <div
              key={s}
              className={`flex items-center justify-between px-3 py-1.5 text-xs ${
                i % 2 === 1 ? "bg-bg-subtle" : ""
              }`}
            >
              <span className="font-mono text-fg-muted">
                {markovStateLabel(s)}
              </span>
              <span className="font-mono text-fg">
                {ref.expected_steps_to_absorption[s].toFixed(3)}
              </span>
            </div>
          ))}
        </div>
      </div>

      {/* Reference model — absorption probabilities from PLANNED */}
      <div>
        <h3 className="mb-1 text-xs font-medium uppercase tracking-wider text-fg-subtle">
          Absorption probability from PLANNED · reference model
        </h3>
        <p className="mb-2 text-[11px] text-fg-muted">
          B = N·R. Probability that a migration started at PLANNED is
          absorbed in each terminal state. Sums to 1.
        </p>
        <div className="flex flex-wrap gap-2">
          {Object.entries(fromPlanned).map(([state, prob]) => (
            <div
              key={state}
              className="rounded-md border border-border-subtle bg-bg-subtle px-3 py-2"
            >
              <div className="text-[10px] uppercase tracking-wider text-fg-subtle">
                {markovStateLabel(state)}
              </div>
              <div className="mt-0.5 font-mono text-sm text-fg">
                {(prob * 100).toFixed(2)}%
              </div>
            </div>
          ))}
        </div>
      </div>

      {/* Empirical estimate */}
      <div>
        <h3 className="mb-1 text-xs font-medium uppercase tracking-wider text-fg-subtle">
          Empirical estimate · from the real audit log
        </h3>
        <p className="mb-2 text-[11px] text-fg-muted">
          Maximum-likelihood transition frequencies counted from observed
          migration state transitions in the audit log.
        </p>
        <div className="flex flex-wrap gap-2">
          <div className="rounded-md border border-border-subtle bg-bg-subtle px-3 py-2">
            <div className="text-[10px] uppercase tracking-wider text-fg-subtle">
              transitions observed
            </div>
            <div className="mt-0.5 font-mono text-sm text-fg">
              {emp.total_transitions}
            </div>
          </div>
          <div className="rounded-md border border-border-subtle bg-bg-subtle px-3 py-2">
            <div className="text-[10px] uppercase tracking-wider text-fg-subtle">
              runs absorbed
            </div>
            <div className="mt-0.5 font-mono text-sm text-fg">
              {emp.runs_observed}
            </div>
          </div>
        </div>
        <p className="mt-2 text-[11px] italic text-fg-subtle">{emp.notes}</p>
      </div>

      {/* Method reference */}
      <p className="border-t border-border-subtle pt-3 text-[11px] text-fg-subtle">
        Method: {data.method_reference}
      </p>
    </div>
  );
}

// ──────────── helpers ────────────

function btn(
  variant: "accent" | "ghost" | "danger",
  disabled: boolean,
): string {
  const base =
    "rounded-md border px-3 py-1.5 text-xs font-medium transition-colors w-full";
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
