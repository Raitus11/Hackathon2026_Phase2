"use client";

import type { GatePlannerInput, MigrationPlanData } from "@/lib/bcl-client";

/**
 * PendingChanges — what the operator will authorise, made visible.
 *
 * Two views of the same planned change, shown at the approval gate so
 * the operator reviews a changeset rather than trusting a paragraph:
 *
 *   1. ChangesetList — an explicit, row-by-row diff: which MQ objects
 *      are NEW, which queues go QLOCAL → QREMOTE, and (critically) the
 *      co-tenant queues that stay UNCHANGED.
 *   2. TopologyDiff — a dependency-free inline-SVG before/after picture
 *      of the app moving off its shared source QM onto a dedicated one.
 *
 * Honesty note: this renders the *planned* change from the plan object
 * — what the engine intends to do — not a live dry-run against the
 * cluster. The heading says "Planned changes" for exactly that reason.
 */

// ─────────────────────────────────────────────────────────────────────
// Changeset diff list
// ─────────────────────────────────────────────────────────────────────

type ChangeKind = "NEW" | "CHANGED" | "UNCHANGED";

interface ChangeRow {
  kind: ChangeKind;
  object: string;
  detail: string;
}

const KIND_STYLE: Record<ChangeKind, { dot: string; text: string; tag: string }> = {
  NEW: {
    dot: "bg-success",
    text: "text-success",
    tag: "NEW",
  },
  CHANGED: {
    dot: "bg-warn",
    text: "text-warn",
    tag: "CHANGED",
  },
  UNCHANGED: {
    dot: "bg-fg-subtle",
    text: "text-fg-muted",
    tag: "UNCHANGED",
  },
};

function buildChangeset(
  plan: MigrationPlanData,
  pin: GatePlannerInput,
): ChangeRow[] {
  const rows: ChangeRow[] = [];

  // Target QM — new only if not already provisioned.
  rows.push({
    kind: pin.target_qm_provisioned ? "UNCHANGED" : "NEW",
    object: pin.target_qm,
    detail: pin.target_qm_provisioned
      ? "dedicated target QM — already provisioned"
      : "dedicated target QM — will be provisioned + MQ-realized",
  });

  // Bridge channel pair + XMITQ — always new for a migration.
  rows.push({
    kind: "NEW",
    object: plan.bridge_channel_name,
    detail: `SDR/RCVR bridge channel pair · ${pin.source_qm} ↔ ${pin.target_qm}`,
  });
  rows.push({
    kind: "NEW",
    object: plan.bridge_xmitq_name,
    detail: `transmission queue on ${pin.source_qm} feeding the bridge`,
  });

  // Each redirected queue: QLOCAL → QREMOTE.
  for (const q of plan.queues_to_redirect) {
    rows.push({
      kind: "CHANGED",
      object: q,
      detail: `QLOCAL → QREMOTE · routed via the bridge to ${pin.target_qm}`,
    });
  }

  return rows;
}

function ChangesetList({
  plan,
  pin,
}: {
  plan: MigrationPlanData;
  pin: GatePlannerInput;
}) {
  const rows = buildChangeset(plan, pin);
  const counts = rows.reduce(
    (acc, r) => {
      acc[r.kind] += 1;
      return acc;
    },
    { NEW: 0, CHANGED: 0, UNCHANGED: 0 } as Record<ChangeKind, number>,
  );

  return (
    <div>
      <div className="mb-2 flex items-baseline gap-3">
        <h4 className="text-xs font-semibold uppercase tracking-wide text-fg-subtle">
          Planned MQSC changeset
        </h4>
        <span className="text-xs text-fg-muted">
          <span className="text-success">{counts.NEW} new</span>
          {" · "}
          <span className="text-warn">{counts.CHANGED} changed</span>
        </span>
      </div>

      <ul className="divide-y divide-border-subtle overflow-hidden rounded-md border border-border-subtle">
        {rows.map((r, i) => {
          const s = KIND_STYLE[r.kind];
          return (
            <li
              key={i}
              className="flex items-start gap-3 bg-bg-subtle px-3 py-2"
            >
              <span
                className={`mt-1.5 h-1.5 w-1.5 shrink-0 rounded-full ${s.dot}`}
              />
              <div className="min-w-0 flex-1">
                <div className="flex items-baseline gap-2">
                  <span
                    className={`shrink-0 text-[10px] font-bold tracking-wide ${s.text}`}
                  >
                    {s.tag}
                  </span>
                  <span className="truncate font-mono text-xs text-fg">
                    {r.object}
                  </span>
                </div>
                <p className="mt-0.5 text-xs text-fg-muted">{r.detail}</p>
              </div>
            </li>
          );
        })}
      </ul>

      {/* Isolation reassurance — the co-tenant queues that stay put. */}
      <p className="mt-2 flex items-center gap-1.5 text-xs text-fg-muted">
        <span className="h-1.5 w-1.5 rounded-full bg-fg-subtle" />
        Every co-tenant queue on{" "}
        <span className="font-mono text-fg-subtle">{pin.source_qm}</span> is
        left untouched — per-app isolation holds.
      </p>
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────
// Before / after topology diagram — dependency-free inline SVG
// ─────────────────────────────────────────────────────────────────────

function TopologyDiff({
  plan,
  pin,
}: {
  plan: MigrationPlanData;
  pin: GatePlannerInput;
}) {
  const queues = plan.queues_to_redirect.slice(0, 5);
  const extra = plan.queues_to_redirect.length - queues.length;
  // Vertical layout sizing.
  const rowH = 22;
  const queuesH = Math.max(queues.length, 1) * rowH;
  const qmBoxH = 64 + queuesH;
  const svgH = qmBoxH + 130;

  const QueueRows = ({
    x,
    y,
    state,
  }: {
    x: number;
    y: number;
    state: "local" | "remote";
  }) =>
    queues.map((q, i) => (
      <g key={q} transform={`translate(${x}, ${y + i * rowH})`}>
        <rect
          width="150"
          height="17"
          rx="3"
          fill={state === "remote" ? "#1d2b3d" : "#202d3b"}
          stroke={state === "remote" ? "#4493f8" : "#3a4c5e"}
          strokeWidth="1"
        />
        <text x="7" y="12" fill="#c9d3de" fontSize="9" fontFamily="monospace">
          {q.length > 16 ? q.slice(0, 15) + "…" : q}
        </text>
        <text
          x="143"
          y="12"
          fill={state === "remote" ? "#4493f8" : "#7d8a99"}
          fontSize="7.5"
          fontWeight="700"
          textAnchor="end"
        >
          {state === "remote" ? "QREMOTE" : "QLOCAL"}
        </text>
      </g>
    ));

  return (
    <div>
      <h4 className="mb-2 text-xs font-semibold uppercase tracking-wide text-fg-subtle">
        Topology — before &amp; after
      </h4>
      <div className="overflow-x-auto rounded-md border border-border-subtle bg-bg-subtle p-3">
        <svg
          viewBox={`0 0 560 ${svgH}`}
          width="100%"
          style={{ minWidth: 480 }}
          fontFamily="'Segoe UI', system-ui, sans-serif"
        >
          <defs>
            <marker
              id="pcArrow"
              markerWidth="9"
              markerHeight="9"
              refX="7"
              refY="3"
              orient="auto"
            >
              <path d="M0,0 L7,3 L0,6 Z" fill="#4493f8" />
            </marker>
          </defs>

          {/* ── BEFORE column ── */}
          <text x="14" y="16" fill="#7d8a99" fontSize="10" fontWeight="700">
            BEFORE
          </text>
          <text x="14" y="30" fill="#586573" fontSize="8.5">
            app shares a source QM
          </text>

          {/* shared source QM */}
          <rect
            x="14"
            y="40"
            width="190"
            height={qmBoxH}
            rx="7"
            fill="#19222d"
            stroke="#33414f"
          />
          <text x="24" y="60" fill="#e6edf3" fontSize="11" fontWeight="600">
            {pin.source_qm}
          </text>
          <text x="24" y="74" fill="#8696a8" fontSize="8">
            shared source QM
          </text>
          <QueueRows x={34} y={84} state="local" />
          {/* co-tenant marker */}
          <text
            x="24"
            y={84 + queuesH + 16}
            fill="#586573"
            fontSize="8"
          >
            + co-tenant apps&apos; queues (unchanged)
          </text>

          {/* ── arrow ── */}
          <line
            x1="210"
            y1={40 + qmBoxH / 2}
            x2="350"
            y2={40 + qmBoxH / 2}
            stroke="#4493f8"
            strokeWidth="1.6"
            markerEnd="url(#pcArrow)"
          />
          <text
            x="280"
            y={40 + qmBoxH / 2 - 22}
            fill="#4493f8"
            fontSize="9"
            fontWeight="600"
            textAnchor="middle"
          >
            migrate {pin.app_id}
          </text>
          <text
            x="280"
            y={40 + qmBoxH / 2 - 9}
            fill="#6f8194"
            fontSize="7.5"
            textAnchor="middle"
          >
            via {plan.bridge_channel_name.length > 22
              ? "SDR/RCVR bridge"
              : plan.bridge_channel_name}
          </text>
          <text
            x="280"
            y={40 + qmBoxH + 14}
            fill="#586573"
            fontSize="7.5"
            textAnchor="middle"
          >
            producers/consumers do not reconnect
          </text>

          {/* ── AFTER column ── */}
          <text x="356" y="16" fill="#7d8a99" fontSize="10" fontWeight="700">
            AFTER
          </text>
          <text x="356" y="30" fill="#586573" fontSize="8.5">
            app on a dedicated QM
          </text>

          {/* dedicated target QM */}
          <rect
            x="356"
            y="40"
            width="190"
            height={qmBoxH}
            rx="7"
            fill="#16202c"
            stroke="#4493f8"
            strokeWidth="1.4"
          />
          <text x="366" y="60" fill="#e6edf3" fontSize="11" fontWeight="600">
            {pin.target_qm}
          </text>
          <text x="366" y="74" fill="#4493f8" fontSize="8">
            dedicated target QM {pin.target_qm_provisioned ? "" : "(new)"}
          </text>
          <QueueRows x={376} y={84} state="remote" />
          <text
            x="366"
            y={84 + queuesH + 16}
            fill="#586573"
            fontSize="8"
          >
            1:1 — this app owns the QM
          </text>

          {/* ── footer summary ── */}
          <text
            x="14"
            y={svgH - 30}
            fill="#7d8a99"
            fontSize="8.5"
          >
            {plan.queues_to_redirect.length} queue
            {plan.queues_to_redirect.length === 1 ? "" : "s"} redirected
            {extra > 0 ? ` (${queues.length} shown, +${extra} more)` : ""} ·
            bridge XMITQ {plan.bridge_xmitq_name}
          </text>
          <text x="14" y={svgH - 16} fill="#586573" fontSize="8">
            Source-side queue names are unchanged — redefined as QREMOTE,
            so the redirect is transparent to applications.
          </text>
        </svg>
      </div>
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────
// Public component
// ─────────────────────────────────────────────────────────────────────

export default function PendingChanges({
  plan,
  plannerInput,
}: {
  plan: MigrationPlanData;
  plannerInput: GatePlannerInput | null;
}) {
  return (
    <div className="panel p-5">
      <div className="mb-3">
        <h3 className="text-sm font-semibold tracking-tight">
          Planned Changes
        </h3>
        <p className="mt-0.5 text-xs text-fg-muted">
          What this migration will change on approval. This is the
          engine&apos;s planned MQSC delta — reviewed before execution,
          not a live cluster dry-run.
        </p>
      </div>

      {plannerInput ? (
        <div className="grid gap-5 lg:grid-cols-2">
          <ChangesetList plan={plan} pin={plannerInput} />
          <TopologyDiff plan={plan} pin={plannerInput} />
        </div>
      ) : (
        <p className="rounded-md border border-warn/30 bg-warn/5 px-3 py-2 text-xs text-warn">
          Planner input is unavailable for this migration — the changeset
          and topology view cannot be rendered. The plan narrative above
          still describes the intended change.
        </p>
      )}
    </div>
  );
}
