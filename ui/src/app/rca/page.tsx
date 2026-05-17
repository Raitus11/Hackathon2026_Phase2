"use client";

/**
 * RCA — Root Cause Analysis workspace.
 *
 * Surfaces agent #3, the RCA Assistant. Pick a migration; the agent
 * reads its Lamport-ordered audit trail, locates the failure event,
 * names the MQ reason code, and returns a structured diagnosis.
 *
 * Also hosts the read-only export downloads: per-migration MQSC script
 * and evidence bundle, plus the global audit-log CSV. These are plain
 * file downloads — the browser fetches the export URL directly.
 *
 * Everything on this page is read-only. The RCA agent issues no MQSC
 * and changes no state; the exports stream existing recorded rows.
 */

import { useEffect, useState } from "react";
import useSWR from "swr";
import {
  bcl,
  migrationStateLabel,
  type Migration,
  type RcaAnswer,
  type RcaReport,
} from "@/lib/bcl-client";

export default function RcaWorkspace() {
  const { data: migrations } = useSWR<Migration[]>(
    "/migrations",
    () => bcl.migrations.list(),
    { refreshInterval: 10000 },
  );

  const [selectedId, setSelectedId] = useState<number | null>(null);

  // Default selection: prefer a failed/rolled-back migration (there is
  // something to diagnose); otherwise the first migration.
  useEffect(() => {
    if (selectedId !== null || !migrations || migrations.length === 0) return;
    const failed = migrations.find(
      (m) => m.state === "ROLLED_BACK" || m.state === "ROLLBACK_FAILED",
    );
    setSelectedId(failed ? failed.id : migrations[0].id);
  }, [migrations, selectedId]);

  return (
    <main className="mx-auto max-w-6xl px-6 py-8">
      <header className="mb-6">
        <h1 className="text-lg font-semibold tracking-tight">
          Root Cause Analysis
        </h1>
        <p className="mt-1 text-sm text-fg-muted">
          Agent #3 — the RCA Assistant. It reads a migration&apos;s
          Lamport-ordered audit trail, finds the failure event, names the
          MQ reason code, and produces a structured diagnosis. The
          narrative is phrased by the LLM (Tachyon / Gemini 2.5 Pro) with
          a deterministic explainer as fallback; the evidence is the same
          either way. Read-only: it issues no MQSC and changes no state.
          Every run is audit-logged as an agent invocation.
        </p>
      </header>

      {/* Migration picker */}
      <section className="panel mb-6 px-4 py-3">
        <div className="mb-2 text-xs font-medium uppercase tracking-wider text-fg-subtle">
          Select a migration to diagnose
        </div>
        <div className="flex flex-wrap gap-2">
          {(migrations ?? []).map((m) => (
            <button
              key={m.id}
              type="button"
              onClick={() => setSelectedId(m.id)}
              className={
                "rounded-md border px-2.5 py-1 text-xs transition-colors " +
                (m.id === selectedId
                  ? "border-accent/50 bg-accent/15 text-accent"
                  : "border-border-subtle bg-bg-subtle text-fg-muted hover:bg-bg-elevated")
              }
            >
              <span className="font-mono">{m.app_id}</span>
              <span className="ml-1.5 text-fg-subtle">
                #{m.id} · {migrationStateLabel(m.state)}
              </span>
            </button>
          ))}
          {migrations && migrations.length === 0 && (
            <span className="text-xs text-fg-subtle">
              No migrations on record yet.
            </span>
          )}
        </div>
      </section>

      {/* RCA report */}
      {selectedId !== null && <RcaPanel migrationId={selectedId} />}

      {/* Free-text question box */}
      <RcaAskPanel />

      {/* Evidence exports for the selected migration */}
      {selectedId !== null && <ExportsPanel migrationId={selectedId} />}
    </main>
  );
}

// ════════════════════════════════════════════════════════════════════════
// Free-text question box.
//
// Sends the question to POST /rca/ask. The backend resolves which
// migration the question is about, runs the same structured diagnosis,
// and answers. On BCL_LLM_PROVIDER=stub the deterministic explainer
// answers (answer_source = "deterministic"); on tachyon the LLM phrases
// it (answer_source = "llm"). The badge shows which — no fake chat.
// ════════════════════════════════════════════════════════════════════════

const RCA_SUGGESTED = [
  "Why did migration 1 fail?",
  "What is the reason code for ZN?",
  "Did APUMN/GC migrate cleanly?",
];

function RcaAskPanel() {
  const [question, setQuestion] = useState("");
  const [answer, setAnswer] = useState<RcaAnswer | null>(null);
  const [pending, setPending] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  async function ask(q: string) {
    const text = q.trim();
    if (!text || pending) return;
    setPending(true);
    setErr(null);
    try {
      const res = await bcl.rca.ask(text);
      setAnswer(res);
    } catch (e) {
      setErr(String(e));
    } finally {
      setPending(false);
    }
  }

  return (
    <section className="panel mb-6">
      <div className="border-b border-border-subtle px-4 py-3">
        <h2 className="text-sm font-medium">Ask the RCA Assistant</h2>
        <p className="mt-0.5 text-[11px] text-fg-muted">
          A free-text question — name an app or a migration id. The
          answer comes from the deterministic explainer, or from the LLM
          (Tachyon / Gemini 2.5 Pro) when{" "}
          <span className="font-mono">BCL_LLM_PROVIDER</span> is set to a
          real provider. The structured evidence is the same either way.
        </p>
      </div>
      <div className="px-4 py-4">
        <div className="flex gap-2">
          <input
            type="text"
            value={question}
            onChange={(e) => setQuestion(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") ask(question);
            }}
            placeholder="e.g. why did migration 3 fail?"
            disabled={pending}
            className="flex-1 rounded-md border border-border-subtle bg-bg-subtle px-3 py-1.5 text-xs text-fg disabled:opacity-50"
          />
          <button
            type="button"
            onClick={() => ask(question)}
            disabled={pending || !question.trim()}
            className="rounded-md border border-accent/40 bg-accent/10 px-3 py-1.5 text-xs font-medium text-accent hover:bg-accent/20 disabled:cursor-not-allowed disabled:opacity-40"
          >
            {pending ? "Asking…" : "Ask"}
          </button>
        </div>

        {/* Suggested questions */}
        <div className="mt-2 flex flex-wrap gap-1.5">
          {RCA_SUGGESTED.map((s) => (
            <button
              key={s}
              type="button"
              onClick={() => {
                setQuestion(s);
                ask(s);
              }}
              disabled={pending}
              className="rounded border border-border-subtle bg-bg-subtle px-2 py-0.5 text-[10px] text-fg-muted hover:bg-bg-elevated disabled:opacity-40"
            >
              {s}
            </button>
          ))}
        </div>

        {err && (
          <p className="mt-3 text-xs text-danger">
            Could not get an answer: {err}
          </p>
        )}

        {answer && (
          <div className="mt-3 rounded-md border border-border-subtle bg-bg-subtle px-3 py-2.5">
            <div className="mb-1 flex flex-wrap items-center gap-2">
              <span className="text-[10px] uppercase tracking-wider text-fg-subtle">
                answer
              </span>
              <span
                className={
                  "rounded bg-bg-elevated px-1.5 py-0.5 text-[10px] uppercase tracking-wider " +
                  (answer.answer_source === "llm"
                    ? "text-accent"
                    : "text-fg-subtle")
                }
              >
                {answer.answer_source === "llm"
                  ? "LLM (Tachyon)"
                  : "deterministic"}
              </span>
              {answer.resolved_migration_id !== null && (
                <span className="rounded bg-bg-elevated px-1.5 py-0.5 text-[10px] uppercase tracking-wider text-fg-subtle">
                  migration #{answer.resolved_migration_id}
                </span>
              )}
            </div>
            <p className="text-xs leading-relaxed text-fg">
              {answer.answer}
            </p>
          </div>
        )}
      </div>
    </section>
  );
}

// ════════════════════════════════════════════════════════════════════════
// Evidence exports — read-only downloads for the selected migration.
// These ALSO appear on the migration detail page; this is an additional
// copy on the RCA tab for convenience, not a move.
// ════════════════════════════════════════════════════════════════════════

function ExportsPanel({ migrationId }: { migrationId: number }) {
  return (
    <section className="panel mb-6 px-4 py-4">
      <h2 className="mb-1 text-sm font-medium">Evidence exports</h2>
      <p className="mb-3 text-[11px] text-fg-muted">
        Read-only downloads for migration #{migrationId}, generated from
        recorded BCL database rows. No MQSC is issued and no state
        changes to produce them.
      </p>
      <div className="flex flex-wrap gap-2">
        <a
          href={bcl.exportUrls.mqscScript(migrationId)}
          className="rounded-md border border-border-subtle bg-bg-subtle px-3 py-1.5 text-xs text-fg hover:bg-bg-elevated"
        >
          ↓ MQSC script (.mqsc)
        </a>
        <a
          href={bcl.exportUrls.evidenceBundle(migrationId)}
          className="rounded-md border border-border-subtle bg-bg-subtle px-3 py-1.5 text-xs text-fg hover:bg-bg-elevated"
        >
          ↓ Evidence bundle (.zip)
        </a>
        <a
          href={bcl.exportUrls.auditCsv()}
          className="rounded-md border border-border-subtle bg-bg-subtle px-3 py-1.5 text-xs text-fg hover:bg-bg-elevated"
        >
          ↓ Audit log (.csv) · full
        </a>
      </div>
    </section>
  );
}

function RcaPanel({ migrationId }: { migrationId: number }) {
  const { data, error, isLoading } = useSWR<RcaReport>(
    `/rca/migrations/${migrationId}`,
    () => bcl.rca.forMigration(migrationId),
    { refreshInterval: 0, revalidateOnFocus: false },
  );

  return (
    <section className="panel mb-6">
      <div className="border-b border-border-subtle px-4 py-3">
        <h2 className="text-sm font-medium">
          RCA Assistant · diagnosis for migration #{migrationId}
        </h2>
      </div>
      <div className="px-4 py-4">
        {isLoading && (
          <p className="py-6 text-center text-xs text-fg-subtle">
            The RCA agent is reading the audit trail…
          </p>
        )}
        {error && (
          <p className="py-6 text-center text-xs text-danger">
            Could not load the RCA report: {String(error)}
          </p>
        )}
        {data && <RcaContent data={data} />}
      </div>
    </section>
  );
}

function RcaContent({ data }: { data: RcaReport }) {
  const conf = data.confidence;
  const confColor =
    conf === "HIGH"
      ? "text-accent"
      : conf === "MEDIUM"
        ? "text-warn"
        : "text-fg-subtle";

  return (
    <div className="space-y-5">
      {/* Verdict banner */}
      <div
        className={
          "rounded-md border px-3 py-2.5 " +
          (data.has_failure
            ? "border-danger/40 bg-danger/10"
            : "border-accent/40 bg-accent/10")
        }
      >
        <div className="flex flex-wrap items-center gap-2">
          <span
            className={
              "text-xs font-semibold " +
              (data.has_failure ? "text-danger" : "text-accent")
            }
          >
            {data.has_failure
              ? "⚠ FAILURE DIAGNOSED"
              : "✓ NO FAILURE TO DIAGNOSE"}
          </span>
          <span className="rounded bg-bg-elevated px-1.5 py-0.5 text-[10px] uppercase tracking-wider text-fg-subtle">
            {data.app_id} · {data.migration_state}
          </span>
          <span
            className={
              "rounded bg-bg-elevated px-1.5 py-0.5 text-[10px] uppercase tracking-wider " +
              confColor
            }
          >
            confidence: {conf}
          </span>
          <span className="rounded bg-bg-elevated px-1.5 py-0.5 text-[10px] uppercase tracking-wider text-fg-subtle">
            narrative: {data.narrative_source}
          </span>
        </div>
        <p className="mt-1.5 text-[11px] leading-relaxed text-fg-muted">
          {data.narrative}
        </p>
      </div>

      {/* Primary hypothesis */}
      <div>
        <h3 className="mb-1 text-xs font-medium uppercase tracking-wider text-fg-subtle">
          Primary hypothesis
        </h3>
        <p className="text-xs text-fg">{data.primary_hypothesis}</p>
        {data.mq_reason_code && (
          <p className="mt-1.5 text-[11px] text-fg-muted">
            MQ reason code{" "}
            <span className="font-mono text-fg">{data.mq_reason_code}</span>
            {data.mq_reason_meaning ? ` — ${data.mq_reason_meaning}` : ""}
          </p>
        )}
      </div>

      {/* Supporting evidence */}
      {data.supporting_evidence.length > 0 && (
        <div>
          <h3 className="mb-1 text-xs font-medium uppercase tracking-wider text-fg-subtle">
            Supporting evidence · from the audit trail
          </h3>
          <div className="overflow-hidden rounded-md border border-border-subtle">
            {data.supporting_evidence.map((e, i) => (
              <div
                key={i}
                className={
                  "px-3 py-1.5 text-xs " +
                  (i % 2 === 1 ? "bg-bg-subtle" : "")
                }
              >
                <div className="flex flex-wrap items-center gap-2">
                  <span className="rounded bg-bg-elevated px-1.5 py-0.5 text-[10px] uppercase tracking-wider text-fg-subtle">
                    {e.source}
                  </span>
                  {e.lamport_clock !== null && (
                    <span className="font-mono text-[11px] text-fg-subtle">
                      L={e.lamport_clock}
                    </span>
                  )}
                  <span className="font-mono text-[11px] text-fg-muted">
                    {e.operation}
                  </span>
                  <span className="ml-auto text-[10px] uppercase tracking-wider text-fg-subtle">
                    relevance: {e.relevance}
                  </span>
                </div>
                <p className="mt-0.5 text-[11px] text-fg">{e.detail}</p>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Contributing factors */}
      {data.contributing_factors.length > 0 && (
        <div>
          <h3 className="mb-1 text-xs font-medium uppercase tracking-wider text-fg-subtle">
            Contributing factors
          </h3>
          <ul className="space-y-1">
            {data.contributing_factors.map((c, i) => (
              <li key={i} className="text-[11px] text-fg-muted">
                · {c}
              </li>
            ))}
          </ul>
        </div>
      )}

      {/* Suggested checks */}
      {data.suggested_checks.length > 0 && (
        <div>
          <h3 className="mb-1 text-xs font-medium uppercase tracking-wider text-fg-subtle">
            Suggested checks · for a human operator
          </h3>
          <ul className="space-y-1">
            {data.suggested_checks.map((c, i) => (
              <li key={i} className="text-[11px] text-fg">
                {i + 1}. {c}
              </li>
            ))}
          </ul>
        </div>
      )}

      {data.references.length > 0 && (
        <p className="border-t border-border-subtle pt-3 text-[11px] text-fg-subtle">
          {data.references.join("  ·  ")}
        </p>
      )}
    </div>
  );
}
