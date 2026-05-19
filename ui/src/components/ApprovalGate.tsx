"use client";

import { useState } from "react";
import {
  bcl,
  type GoNoGoDecision,
  type MigrationGate,
  type PreflightFinding,
  type PreflightRiskBrief,
} from "@/lib/bcl-client";
import PendingChanges from "@/components/PendingChanges";

/**
 * ApprovalGate — the human approval checkpoint.
 *
 * Rendered when a migration is parked in AWAITING_APPROVAL. Shows the
 * Migration Planner's plan, the Pre-Flight Risk Auditor's risk brief,
 * and the decision-theoretic go/no-go score, then offers the operator
 * three actions: Approve (resume the forward path), Revise (re-plan
 * with a free-text instruction — the loop stays at the gate), or
 * Abort (route through rollback to ROLLED_BACK).
 *
 * "AI proposes, human disposes": every destructive MQSC command
 * downstream of this component is gated on the operator's click here.
 */

const SEV_COLOR: Record<PreflightFinding["severity"], string> = {
  CRITICAL: "text-danger",
  HIGH: "text-danger",
  MEDIUM: "text-warn",
  LOW: "text-fg-muted",
};

const SEV_DOT: Record<PreflightFinding["severity"], string> = {
  CRITICAL: "bg-danger",
  HIGH: "bg-danger",
  MEDIUM: "bg-warn",
  LOW: "bg-fg-subtle",
};

const REC_STYLE: Record<
  GoNoGoDecision["recommendation"],
  { label: string; cls: string }
> = {
  PROCEED: {
    label: "PROCEED",
    cls: "border-success/40 bg-success/10 text-success",
  },
  PROCEED_WITH_CAUTION: {
    label: "PROCEED · WITH CAUTION",
    cls: "border-warn/40 bg-warn/10 text-warn",
  },
  DEFER: {
    label: "DEFER",
    cls: "border-danger/40 bg-danger/10 text-danger",
  },
};

function pct(x: number): string {
  return `${(x * 100).toFixed(1)}%`;
}

// ─────────────────────────────────────────────────────────────────────
// Go/No-Go score panel
// ─────────────────────────────────────────────────────────────────────

function GoNoGoPanel({ d }: { d: GoNoGoDecision }) {
  const rec = REC_STYLE[d.recommendation];
  const proceedCheaper = d.advantage > 0;

  return (
    <div className="panel p-5">
      <div className="mb-4 flex items-center justify-between">
        <div>
          <h3 className="text-sm font-semibold tracking-tight">
            Go / No-Go Decision Score
          </h3>
          <p className="mt-0.5 text-xs text-fg-muted">
            Expected-cost comparison over the migration&apos;s absorbing
            Markov chain
          </p>
        </div>
        <span
          className={`rounded-md border px-3 py-1 text-xs font-semibold ${rec.cls}`}
        >
          {rec.label}
        </span>
      </div>

      {/* Expected cost bars */}
      <div className="space-y-3">
        <CostBar
          label="E[cost | PROCEED]"
          value={d.expected_cost_proceed}
          max={Math.max(d.expected_cost_proceed, d.expected_cost_defer)}
          highlight={proceedCheaper}
        />
        <CostBar
          label="E[cost | DEFER]"
          value={d.expected_cost_defer}
          max={Math.max(d.expected_cost_proceed, d.expected_cost_defer)}
          highlight={!proceedCheaper}
        />
      </div>

      <p className="mt-3 text-xs text-fg-muted">
        {proceedCheaper ? "Proceeding" : "Deferring"} is cheaper in
        expectation by{" "}
        <span className="font-mono text-fg">
          {Math.abs(d.advantage).toFixed(2)}
        </span>{" "}
        cost units · confidence{" "}
        <span className="font-mono text-fg">{pct(d.confidence)}</span>
      </p>

      {/* Outcome distribution */}
      <div className="mt-4 border-t border-border-subtle pt-3">
        <p className="mb-2 text-xs font-medium text-fg-subtle">
          Outcome distribution if approved (Markov absorption
          probabilities)
        </p>
        <div className="grid grid-cols-3 gap-2 text-center">
          <DistCell
            label="completed"
            value={d.outcome_distribution.p_completed}
            tone="text-success"
          />
          <DistCell
            label="clean rollback"
            value={d.outcome_distribution.p_rolled_back}
            tone="text-warn"
          />
          <DistCell
            label="stuck rollback"
            value={d.outcome_distribution.p_rollback_failed}
            tone="text-danger"
          />
        </div>
      </div>

      {/* Method note */}
      <details className="mt-3 text-xs text-fg-muted">
        <summary className="cursor-pointer hover:text-fg">
          method &amp; references
        </summary>
        <p className="mt-2 leading-relaxed">{d.rationale}</p>
        <ul className="mt-2 space-y-0.5">
          {d.references.map((r) => (
            <li key={r} className="text-fg-subtle">
              · {r}
            </li>
          ))}
        </ul>
      </details>
    </div>
  );
}

function CostBar({
  label,
  value,
  max,
  highlight,
}: {
  label: string;
  value: number;
  max: number;
  highlight: boolean;
}) {
  const width = max > 0 ? Math.max(4, (value / max) * 100) : 4;
  return (
    <div>
      <div className="mb-1 flex items-baseline justify-between text-xs">
        <span className="text-fg-subtle">{label}</span>
        <span className="font-mono text-fg">{value.toFixed(3)}</span>
      </div>
      <div className="h-2 overflow-hidden rounded-full bg-bg-subtle">
        <div
          className={`h-full rounded-full ${
            highlight ? "bg-accent" : "bg-fg-subtle/40"
          }`}
          style={{ width: `${width}%` }}
        />
      </div>
    </div>
  );
}

function DistCell({
  label,
  value,
  tone,
}: {
  label: string;
  value: number;
  tone: string;
}) {
  return (
    <div className="rounded-md border border-border-subtle bg-bg-subtle px-2 py-2">
      <p className={`font-mono text-sm font-semibold ${tone}`}>
        {pct(value)}
      </p>
      <p className="mt-0.5 text-[10px] uppercase tracking-wide text-fg-muted">
        {label}
      </p>
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────
// Risk brief panel
// ─────────────────────────────────────────────────────────────────────

function RiskBriefPanel({ brief }: { brief: PreflightRiskBrief }) {
  const assessmentCls =
    brief.overall_assessment === "REVIEW_BEFORE_APPROVING"
      ? "text-danger"
      : brief.overall_assessment === "PROCEED_WITH_CARE"
        ? "text-warn"
        : "text-success";

  return (
    <div className="panel p-5">
      <div className="mb-3 flex items-center justify-between">
        <h3 className="text-sm font-semibold tracking-tight">
          Pre-Flight Risk Brief
        </h3>
        <span className={`text-xs font-semibold ${assessmentCls}`}>
          {brief.overall_assessment.replace(/_/g, " ")}
        </span>
      </div>
      <p className="mb-3 text-xs leading-relaxed text-fg-muted">
        {brief.summary}
      </p>

      {brief.findings.length === 0 ? (
        <p className="rounded-md border border-success/30 bg-success/5 px-3 py-2 text-xs text-success">
          No hazards above LOW severity. The auditor found nothing
          requiring review.
        </p>
      ) : (
        <ul className="space-y-2">
          {brief.findings.map((f, i) => (
            <li
              key={i}
              className="rounded-md border border-border-subtle bg-bg-subtle p-3"
            >
              <div className="flex items-center gap-2">
                <span
                  className={`h-1.5 w-1.5 rounded-full ${SEV_DOT[f.severity]}`}
                />
                <span
                  className={`text-xs font-semibold ${SEV_COLOR[f.severity]}`}
                >
                  {f.severity}
                </span>
                <span className="text-[10px] uppercase tracking-wide text-fg-muted">
                  {f.category.replace(/_/g, " ")}
                </span>
              </div>
              <p className="mt-1.5 text-xs font-medium text-fg">
                {f.title}
              </p>
              <p className="mt-1 text-xs leading-relaxed text-fg-muted">
                {f.detail}
              </p>
              <p className="mt-1.5 text-xs leading-relaxed text-accent">
                → {f.recommendation}
              </p>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────
// Main component
// ─────────────────────────────────────────────────────────────────────

type Busy = "approve" | "abort" | "revise" | null;

export default function ApprovalGate({
  gate,
  operator,
  onResolved,
  onRevised,
}: {
  gate: MigrationGate;
  operator: string;
  /** Called after approve/abort — parent should re-poll the migration. */
  onResolved: () => void;
  /** Called after a successful revise — parent should re-fetch the gate. */
  onRevised: () => void;
}) {
  const [busy, setBusy] = useState<Busy>(null);
  const [error, setError] = useState<string | null>(null);
  const [reviseOpen, setReviseOpen] = useState(false);
  const [abortOpen, setAbortOpen] = useState(false);
  const [instruction, setInstruction] = useState("");
  const [abortReason, setAbortReason] = useState("");

  const mid = gate.migration_id;
  const disabled = busy !== null;

  async function doApprove() {
    setBusy("approve");
    setError(null);
    try {
      await bcl.migrations.approve(mid, operator);
      onResolved();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setBusy(null);
    }
  }

  async function doAbort() {
    if (!abortReason.trim()) {
      setError("An abort reason is required.");
      return;
    }
    setBusy("abort");
    setError(null);
    try {
      await bcl.migrations.abort(mid, operator, abortReason.trim());
      onResolved();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setBusy(null);
    }
  }

  async function doRevise() {
    if (!instruction.trim()) {
      setError("Enter a revision instruction.");
      return;
    }
    setBusy("revise");
    setError(null);
    try {
      await bcl.migrations.revise(mid, operator, instruction.trim());
      setInstruction("");
      setReviseOpen(false);
      onRevised();
      setBusy(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setBusy(null);
    }
  }

  return (
    <section className="space-y-4">
      {/* Gate banner */}
      <div className="rounded-lg border border-accent/40 bg-accent/5 px-5 py-4">
        <div className="flex items-center gap-2">
          <span className="h-2 w-2 animate-pulse rounded-full bg-accent" />
          <h2 className="text-sm font-semibold tracking-tight text-accent">
            Human Approval Gate
          </h2>
        </div>
        <p className="mt-1 text-xs text-fg-muted">
          The plan and risk brief are ready. The migration is paused —
          no MQSC will run until an operator decides. AI proposes;
          you dispose.
        </p>
        {gate.revision_history.length > 0 && (
          <p className="mt-1.5 text-xs text-fg-subtle">
            Plan revised {gate.revision_history.length}×
            {" · "}latest instruction:{" "}
            <span className="italic text-fg-muted">
              &ldquo;
              {
                gate.revision_history[gate.revision_history.length - 1]
                  .instruction
              }
              &rdquo;
            </span>
          </p>
        )}
      </div>

      {/* Go/No-Go + Risk brief side by side */}
      <div className="grid gap-4 lg:grid-cols-2">
        {gate.go_no_go && <GoNoGoPanel d={gate.go_no_go} />}
        {gate.risk_brief && <RiskBriefPanel brief={gate.risk_brief} />}
      </div>

      {/* Plan narrative */}
      {gate.plan && (
        <div className="panel p-5">
          <h3 className="mb-2 text-sm font-semibold tracking-tight">
            Migration Plan
          </h3>
          <p className="text-xs leading-relaxed text-fg-muted">
            {gate.plan.narrative}
          </p>
          <p className="mt-2 text-xs leading-relaxed text-fg-subtle">
            <span className="font-medium text-fg-muted">
              Ordering rationale:{" "}
            </span>
            {gate.plan.ordering_rationale}
          </p>
          <div className="mt-3 flex flex-wrap gap-x-6 gap-y-1 text-xs text-fg-muted">
            <span>
              bridge:{" "}
              <span className="font-mono text-fg-subtle">
                {gate.plan.bridge_channel_name}
              </span>
            </span>
            <span>
              queues to redirect:{" "}
              <span className="font-mono text-fg-subtle">
                {gate.plan.queues_to_redirect.length}
              </span>
            </span>
            <span>
              est. duration:{" "}
              <span className="font-mono text-fg-subtle">
                {gate.plan.predicted_duration_seconds}s
              </span>
            </span>
          </div>
        </div>
      )}

      {/* Planned changes — changeset diff + before/after topology */}
      {gate.plan && (
        <PendingChanges
          plan={gate.plan}
          plannerInput={gate.planner_input}
        />
      )}

      {/* Error surface */}
      {error && (
        <div className="rounded-md border border-danger/40 bg-danger/10 px-3 py-2 text-xs text-danger">
          {error}
        </div>
      )}

      {/* Decision actions */}
      <div className="panel p-5">
        <h3 className="mb-3 text-sm font-semibold tracking-tight">
          Operator Decision
        </h3>
        <div className="flex flex-wrap gap-2">
          <button
            onClick={doApprove}
            disabled={disabled}
            className={`rounded-md border px-4 py-2 text-xs font-semibold transition-colors ${
              disabled
                ? "cursor-not-allowed border-border-subtle bg-bg-subtle text-fg-muted opacity-50"
                : "border-success/40 bg-success/10 text-success hover:bg-success/20"
            }`}
          >
            {busy === "approve" ? "Approving…" : "Approve & Execute"}
          </button>
          <button
            onClick={() => {
              setReviseOpen((v) => !v);
              setAbortOpen(false);
              setError(null);
            }}
            disabled={disabled}
            className={`rounded-md border px-4 py-2 text-xs font-semibold transition-colors ${
              disabled
                ? "cursor-not-allowed border-border-subtle bg-bg-subtle text-fg-muted opacity-50"
                : "border-accent/40 bg-accent/10 text-accent hover:bg-accent/20"
            }`}
          >
            Revise Plan
          </button>
          <button
            onClick={() => {
              setAbortOpen((v) => !v);
              setReviseOpen(false);
              setError(null);
            }}
            disabled={disabled}
            className={`rounded-md border px-4 py-2 text-xs font-semibold transition-colors ${
              disabled
                ? "cursor-not-allowed border-border-subtle bg-bg-subtle text-fg-muted opacity-50"
                : "border-danger/40 bg-danger/10 text-danger hover:bg-danger/20"
            }`}
          >
            Abort
          </button>
        </div>

        {/* Revise panel — the re-plan loop */}
        {reviseOpen && (
          <div className="mt-4 border-t border-border-subtle pt-4">
            <label className="text-xs font-medium text-fg-subtle">
              Revision instruction
            </label>
            <p className="mt-0.5 text-xs text-fg-muted">
              Folded into the planner as advisory guidance. The
              planner, risk auditor, and go/no-go score all re-run;
              the migration stays at the gate. The instruction cannot
              change the bridge naming or the queues — those are fixed
              by the engine.
            </p>
            <textarea
              value={instruction}
              onChange={(e) => setInstruction(e.target.value)}
              placeholder='e.g. "Treat the mainframe co-tenant as a HIGH risk and explain the rollback window constraint."'
              rows={3}
              className="mt-2 w-full rounded-md border border-border-subtle bg-bg-subtle px-3 py-2 text-xs text-fg placeholder:text-fg-muted focus:border-accent focus:outline-none"
            />
            <button
              onClick={doRevise}
              disabled={disabled}
              className={`mt-2 rounded-md border px-3 py-1.5 text-xs font-medium transition-colors ${
                disabled
                  ? "cursor-not-allowed border-border-subtle bg-bg-subtle text-fg-muted opacity-50"
                  : "border-accent/40 bg-accent/10 text-accent hover:bg-accent/20"
              }`}
            >
              {busy === "revise" ? "Re-planning…" : "Submit Revision"}
            </button>
          </div>
        )}

        {/* Abort panel */}
        {abortOpen && (
          <div className="mt-4 border-t border-border-subtle pt-4">
            <label className="text-xs font-medium text-fg-subtle">
              Abort reason
            </label>
            <p className="mt-0.5 text-xs text-fg-muted">
              Routes through the rollback engine — nothing has been
              provisioned yet, so the migration settles cleanly in
              ROLLED_BACK. The reason is audit-logged.
            </p>
            <input
              value={abortReason}
              onChange={(e) => setAbortReason(e.target.value)}
              placeholder="why this migration is being aborted at the gate"
              className="mt-2 w-full rounded-md border border-border-subtle bg-bg-subtle px-3 py-2 text-xs text-fg placeholder:text-fg-muted focus:border-danger focus:outline-none"
            />
            <button
              onClick={doAbort}
              disabled={disabled}
              className={`mt-2 rounded-md border px-3 py-1.5 text-xs font-medium transition-colors ${
                disabled
                  ? "cursor-not-allowed border-border-subtle bg-bg-subtle text-fg-muted opacity-50"
                  : "border-danger/40 bg-danger/10 text-danger hover:bg-danger/20"
              }`}
            >
              {busy === "abort" ? "Aborting…" : "Confirm Abort"}
            </button>
          </div>
        )}
      </div>
    </section>
  );
}
