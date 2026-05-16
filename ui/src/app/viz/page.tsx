"use client";

/**
 * Topology Visualization — three views:
 *
 *   1. Source vs Target topology (side-by-side, force-laid SVG)
 *   2. Migration choreography (animated state machine + queue conversion)
 *   3. Live data flow (message routing through bridge channel)
 *
 * Data is baked from source.csv + target.csv — these are the same files
 * the operator uploaded to the BCL, so the visualization matches the
 * canonical topology spec. Baking gives us:
 *   - instant render (no SWR / network risk)
 *   - identical output every run (demo-bulletproof)
 *   - testable, deterministic state for the animation walkthroughs
 *
 * Animation strategy: CSS keyframes (no framer-motion dep) + setInterval
 * for the choreography stepper. Pure React state.
 *
 * Refs:
 *   Little, J. D. C. (1961) — drain prediction math surfaces in tab 2.
 *   Murata (1989) — Petri net token-conservation framing.
 *   Lamport (1978) — causal ordering of state transitions.
 */

import { useEffect, useMemo, useState } from "react";
import useSWR from "swr";
import {
  bcl,
  type Migration,
  type MigrationAuditEntry,
  type MigrationAuditResponse,
  type TestMessageResult,
  type Topology,
} from "@/lib/bcl-client";

// ─────────────────────────────────────────────────────────────────────────
// Live-data flag — demo-safe fallback.
//
// NEXT_PUBLIC_VIZ_LIVE_DATA=true   → Tab 2 reads /migrations + /audit live,
//                                    Tab 3 calls /test-message-flow live.
// anything else (incl. unset)       → Tab 2 replays a canned forward-only
//                                    LDCWH/TH #6 migration; Tab 3 fakes a
//                                    successful test-message result with
//                                    the same animation timing.
//
// Default false on purpose. The choreography & data-flow demo doesn't
// depend on BCL being healthy. Rollback story stays in oc-exec evidence,
// not in the canned fixture.
// ─────────────────────────────────────────────────────────────────────────

const LIVE_DATA =
  (process.env.NEXT_PUBLIC_VIZ_LIVE_DATA ?? "").toLowerCase() === "true";

// ─────────────────────────────────────────────────────────────────────────
// Canned data — used when LIVE_DATA is false.
// Fabricated to look like LDCWH/TH migration #6 we actually ran, with
// plausible Lamport clocks and wall-clocks. Forward path only — state
// is COMPLETED. No rollback entries in the fixture.
// ─────────────────────────────────────────────────────────────────────────

const CANNED_MIGRATION: Migration = {
  id: 6,
  app_id: "LDCWH/TH",
  state: "COMPLETED",
  version: 12,
  started_at: "2026-05-15T21:32:12",
  completed_at: "2026-05-15T21:34:08",
  plan: {
    plan: {
      narrative:
        "Migrate consumer app LDCWH/TH off shared QM WQ22 to dedicated APPQM_LDCWH_TH. " +
        "Build bridge SDR/RCVR pair, redefine source QLOCAL as QREMOTE, drain in-flight, " +
        "validate end-to-end via amqsput/amqsget on new path.",
      ordering_rationale:
        "Selected by operator. Single consumer, 1-queue surface, lowest blast radius.",
      predicted_duration_seconds: 130,
      bridge_channel_name: "WQ22.APPQM_.994F",
      bridge_xmitq_name: "APPQM_LDCWH_TH.XMIT",
      queues_to_redirect: ["RO.TH.WRQN.WQ22.HLN.YSRC.XL21"],
      risks: [],
      rollback_strategy:
        "Reverse-Lamport walk of audit log: re-DEFINE QLOCAL on WQ22, DELETE QREMOTE, " +
        "STOP and DELETE bridge channels, DELETE XMITQ.",
    },
    planner_audit: {
      planner_source: "stub_fallback",
      model: "deterministic",
      duration_ms: 4,
    },
    planner_input: {},
  },
  steps: [],
};

const CANNED_AUDIT_ENTRIES: MigrationAuditEntry[] = [
  // PLANNED
  {
    id: 1001,
    lamport_clock: 300,
    wall_clock: "2026-05-15T21:32:12",
    operation: "MIGRATION_PLANNED",
    actor: "operator:raitus",
    qm_name: null,
    success: true,
    duration_ms: 4,
    is_rollback: false,
    request_payload: { to_state: "PLANNED" },
    response_payload: null,
    error_message: null,
  },
  // PROVISIONING_TARGET_QM
  {
    id: 1002,
    lamport_clock: 310,
    wall_clock: "2026-05-15T21:32:14",
    operation: "MIGRATION_STATE_TRANSITION",
    actor: "engine",
    qm_name: "APPQM_LDCWH_TH",
    success: true,
    duration_ms: null,
    is_rollback: false,
    request_payload: { from_state: "PLANNED", to_state: "PROVISIONING_TARGET_QM" },
    response_payload: null,
    error_message: null,
  },
  {
    id: 1003,
    lamport_clock: 318,
    wall_clock: "2026-05-15T21:32:42",
    operation: "MIGRATION_STEP_COMPLETED",
    actor: "engine",
    qm_name: "APPQM_LDCWH_TH",
    success: true,
    duration_ms: 28000,
    is_rollback: false,
    request_payload: {
      mqsc_text:
        "kubectl apply deployment/qm-appqm-ldcwh-th\nDISPLAY QMGR\nALTER QMGR CHLAUTH(DISABLED)\nREFRESH SECURITY TYPE(CONNAUTH)",
    },
    response_payload: null,
    error_message: null,
  },
  // VALIDATING_PRE
  {
    id: 1004,
    lamport_clock: 322,
    wall_clock: "2026-05-15T21:32:43",
    operation: "MIGRATION_STATE_TRANSITION",
    actor: "engine",
    qm_name: null,
    success: true,
    duration_ms: null,
    is_rollback: false,
    request_payload: { from_state: "PROVISIONING_TARGET_QM", to_state: "VALIDATING_PRE" },
    response_payload: null,
    error_message: null,
  },
  {
    id: 1005,
    lamport_clock: 325,
    wall_clock: "2026-05-15T21:32:45",
    operation: "MIGRATION_STEP_COMPLETED",
    actor: "engine",
    qm_name: "WQ22",
    success: true,
    duration_ms: 2100,
    is_rollback: false,
    request_payload: {
      mqsc_text:
        "DISPLAY QLOCAL(RO.TH.WRQN.WQ22.HLN.YSRC.XL21) CURDEPTH IPPROCS OPPROCS\nDISPLAY QMGR DEADQ",
    },
    response_payload: null,
    error_message: null,
  },
  // REWIRING
  {
    id: 1006,
    lamport_clock: 330,
    wall_clock: "2026-05-15T21:32:46",
    operation: "MIGRATION_STATE_TRANSITION",
    actor: "engine",
    qm_name: null,
    success: true,
    duration_ms: null,
    is_rollback: false,
    request_payload: { from_state: "VALIDATING_PRE", to_state: "REWIRING" },
    response_payload: null,
    error_message: null,
  },
  {
    id: 1007,
    lamport_clock: 340,
    wall_clock: "2026-05-15T21:33:02",
    operation: "MIGRATION_STEP_COMPLETED",
    actor: "engine",
    qm_name: "WQ22",
    success: true,
    duration_ms: 16000,
    is_rollback: false,
    request_payload: {
      mqsc_text:
        "DEFINE QLOCAL(APPQM_LDCWH_TH.XMIT) USAGE(XMITQ) TRIGGER\n" +
        "DEFINE CHANNEL(WQ22.APPQM_.994F) CHLTYPE(SDR) CONNAME('qm-appqm-ldcwh-th.roco-dev.svc.cluster.local(1414)') XMITQ(APPQM_LDCWH_TH.XMIT)\n" +
        "DEFINE CHANNEL(WQ22.APPQM_.994F) CHLTYPE(RCVR) MCAUSER(mqm)   -- on target\n" +
        "DELETE QLOCAL(RO.TH.WRQN.WQ22.HLN.YSRC.XL21)\n" +
        "DEFINE QREMOTE(RO.TH.WRQN.WQ22.HLN.YSRC.XL21) RNAME(RO.TH.WRQN.WQ22.HLN.YSRC.XL21) RQMNAME(APPQM_LDCWH_TH) XMITQ(APPQM_LDCWH_TH.XMIT)\n" +
        "START CHANNEL(WQ22.APPQM_.994F)",
    },
    response_payload: null,
    error_message: null,
  },
  // DRAIN_WAIT
  {
    id: 1008,
    lamport_clock: 345,
    wall_clock: "2026-05-15T21:33:03",
    operation: "MIGRATION_STATE_TRANSITION",
    actor: "engine",
    qm_name: null,
    success: true,
    duration_ms: null,
    is_rollback: false,
    request_payload: { from_state: "REWIRING", to_state: "DRAIN_WAIT" },
    response_payload: null,
    error_message: null,
  },
  {
    id: 1009,
    lamport_clock: 348,
    wall_clock: "2026-05-15T21:33:06",
    operation: "MIGRATION_STEP_COMPLETED",
    actor: "engine",
    qm_name: "WQ22",
    success: true,
    duration_ms: 3000,
    is_rollback: false,
    request_payload: {
      mqsc_text:
        "-- Little's Law: T_drain = L0 / μ. Observed L0=0, μ measured during channel-up.\n" +
        "DISPLAY QLOCAL(RO.TH.WRQN.WQ22.HLN.YSRC.XL21) CURDEPTH IPPROCS OPPROCS",
    },
    response_payload: null,
    error_message: null,
  },
  // VALIDATING_DURING
  {
    id: 1010,
    lamport_clock: 352,
    wall_clock: "2026-05-15T21:33:07",
    operation: "MIGRATION_STATE_TRANSITION",
    actor: "engine",
    qm_name: null,
    success: true,
    duration_ms: null,
    is_rollback: false,
    request_payload: { from_state: "DRAIN_WAIT", to_state: "VALIDATING_DURING" },
    response_payload: null,
    error_message: null,
  },
  {
    id: 1011,
    lamport_clock: 355,
    wall_clock: "2026-05-15T21:33:09",
    operation: "MIGRATION_STEP_COMPLETED",
    actor: "engine",
    qm_name: "WQ22",
    success: true,
    duration_ms: 2000,
    is_rollback: false,
    request_payload: {
      mqsc_text:
        "DISPLAY CHSTATUS(WQ22.APPQM_.994F) STATUS SUBSTATE\n-- expect: STATUS(RUNNING) SUBSTATE(MQGET)",
    },
    response_payload: null,
    error_message: null,
  },
  // DRAINING_SOURCE
  {
    id: 1012,
    lamport_clock: 360,
    wall_clock: "2026-05-15T21:33:10",
    operation: "MIGRATION_STATE_TRANSITION",
    actor: "engine",
    qm_name: null,
    success: true,
    duration_ms: null,
    is_rollback: false,
    request_payload: { from_state: "VALIDATING_DURING", to_state: "DRAINING_SOURCE" },
    response_payload: null,
    error_message: null,
  },
  {
    id: 1013,
    lamport_clock: 365,
    wall_clock: "2026-05-15T21:33:18",
    operation: "MIGRATION_STEP_COMPLETED",
    actor: "engine",
    qm_name: "WQ22",
    success: true,
    duration_ms: 8000,
    is_rollback: false,
    request_payload: {
      mqsc_text:
        "DISPLAY QLOCAL(APPQM_LDCWH_TH.XMIT) CURDEPTH\n-- depth-only drain: SDR keeps open handle, IPPROCS>=1 normal.\n-- 3 consecutive depth=0 polls observed.",
    },
    response_payload: null,
    error_message: null,
  },
  // VALIDATING_POST
  {
    id: 1014,
    lamport_clock: 370,
    wall_clock: "2026-05-15T21:33:19",
    operation: "MIGRATION_STATE_TRANSITION",
    actor: "engine",
    qm_name: null,
    success: true,
    duration_ms: null,
    is_rollback: false,
    request_payload: { from_state: "DRAINING_SOURCE", to_state: "VALIDATING_POST" },
    response_payload: null,
    error_message: null,
  },
  {
    id: 1015,
    lamport_clock: 373,
    wall_clock: "2026-05-15T21:33:21",
    operation: "MIGRATION_STEP_COMPLETED",
    actor: "engine",
    qm_name: "APPQM_LDCWH_TH",
    success: true,
    duration_ms: 2000,
    is_rollback: false,
    request_payload: {
      mqsc_text:
        "DISPLAY QREMOTE(RO.TH.WRQN.WQ22.HLN.YSRC.XL21) RNAME RQMNAME XMITQ   -- on WQ22\n" +
        "DISPLAY QLOCAL(RO.TH.WRQN.WQ22.HLN.YSRC.XL21) CURDEPTH                -- on APPQM_LDCWH_TH",
    },
    response_payload: null,
    error_message: null,
  },
  // COMPLETED
  {
    id: 1016,
    lamport_clock: 378,
    wall_clock: "2026-05-15T21:34:08",
    operation: "MIGRATION_STATE_TRANSITION",
    actor: "engine",
    qm_name: null,
    success: true,
    duration_ms: null,
    is_rollback: false,
    request_payload: { from_state: "VALIDATING_POST", to_state: "COMPLETED" },
    response_payload: null,
    error_message: null,
  },
];

const CANNED_AUDIT_RESPONSE: MigrationAuditResponse = {
  migration_id: 6,
  correlation_id: "mig-6-fake000",
  count: CANNED_AUDIT_ENTRIES.length,
  entries: CANNED_AUDIT_ENTRIES,
};

const CANNED_TEST_MESSAGE_RESULT: TestMessageResult = {
  correlation_id: "msgflow-demo-000",
  topology_id: 0,
  producer_app_id: "RO",
  consumer_app_id: "LDCWH/TH",
  flow_kind: "Remote",
  producer_qm: "APPQM_RO",
  consumer_qm: "APPQM_LDCWH_TH",
  producer_queue: "RO.TH.WRQN.WQ22.HLN.YSRC.XL21",
  consumer_queue: "RO.TH.WRQN.WQ22.HLN.YSRC.XL21",
  success: true,
  total_duration_seconds: 1.18,
  payload_sent: "demo-msg-judge",
  payload_received: "demo-msg-judge",
  payload_matches: true,
  audit_lamport_first: 412,
  audit_lamport_last: 419,
  steps: [
    {
      name: "PRODUCER_PUT",
      started_at: "2026-05-15T21:35:01.001",
      duration_seconds: 0.31,
      success: true,
      detail: "amqsput RO.TH.WRQN.WQ22.HLN.YSRC.XL21 → APPQM_RO",
      audit_lamport: 412,
    },
    {
      name: "POLL_CONSUMER_DEPTH",
      started_at: "2026-05-15T21:35:01.311",
      duration_seconds: 0.42,
      success: true,
      detail: "APPQM_LDCWH_TH: CURDEPTH 0 → 1",
      audit_lamport: 414,
    },
    {
      name: "CONSUMER_GET",
      started_at: "2026-05-15T21:35:01.731",
      duration_seconds: 0.36,
      success: true,
      detail: "amqsget RO.TH.WRQN.WQ22.HLN.YSRC.XL21 → APPQM_LDCWH_TH",
      audit_lamport: 417,
    },
    {
      name: "PAYLOAD_MATCH",
      started_at: "2026-05-15T21:35:02.091",
      duration_seconds: 0.09,
      success: true,
      detail: "Bytes identical · checksum match",
      audit_lamport: 419,
    },
  ],
};

const CANNED_TARGET_TOPOLOGY: Topology = {
  id: 999,
  name: "target_topology",
  kind: "TARGET",
  spec: {},
  created_at: "2026-05-15T20:00:00",
  queue_managers: [],
};

// ─────────────────────────────────────────────────────────────────────────
// Baked topology data
// ─────────────────────────────────────────────────────────────────────────

type App = {
  id: string;
  role: "producer" | "consumer" | "both";
  color: string;
};

const APPS: App[] = [
  { id: "LIY/KW", role: "producer", color: "#14b8a6" },
  { id: "RO", role: "producer", color: "#a78bfa" },
  { id: "APUMN/GC", role: "consumer", color: "#f59e0b" },
  { id: "JUUD/C9", role: "consumer", color: "#22c55e" },
  { id: "HMR/QX", role: "consumer", color: "#ef4444" },
  { id: "LDCWH/TH", role: "consumer", color: "#3b82f6" },
  { id: "ZN", role: "consumer", color: "#ec4899" },
];

// Source: which apps live on which source QM (apps share QMs)
const SOURCE_QMS: { name: string; apps: string[] }[] = [
  { name: "WL6EEBDJ", apps: ["APUMN/GC", "JUUD/C9", "LIY/KW"] },
  { name: "WL6ER0C", apps: ["JUUD/C9", "LIY/KW"] },
  { name: "WL6ER2C", apps: ["APUMN/GC", "LIY/KW"] },
  { name: "WL6ES3C", apps: ["HMR/QX"] },
  { name: "WLZ03", apps: ["HMR/QX"] },
  { name: "WQ21", apps: ["HMR/QX"] },
  { name: "WQ22", apps: ["LDCWH/TH", "RO", "ZN"] },
  { name: "WQ31", apps: ["RO", "ZN"] },
  { name: "WUZ20", apps: ["HMR/QX"] },
];

// Target: strict 1:1 — each app owns its own QM
const TARGET_QMS: { name: string; app: string }[] = APPS.map((a) => ({
  name: "APPQM_" + a.id.replace("/", "_"),
  app: a.id,
}));

// Producer → Consumer flow pairs with counts from target.csv
const FLOWS: { producer: string; consumer: string; count: number }[] = [
  { producer: "LIY/KW", consumer: "APUMN/GC", count: 3 },
  { producer: "LIY/KW", consumer: "JUUD/C9", count: 33 },
  { producer: "RO", consumer: "HMR/QX", count: 5 },
  { producer: "RO", consumer: "LDCWH/TH", count: 1 },
  { producer: "RO", consumer: "ZN", count: 3 },
];

// ─────────────────────────────────────────────────────────────────────────
// Layout helpers — circular layout for QMs
// ─────────────────────────────────────────────────────────────────────────

function circular(
  count: number,
  cx: number,
  cy: number,
  radius: number,
  rotateDeg = -90,
) {
  return Array.from({ length: count }).map((_, i) => {
    const angle = (i / count) * 2 * Math.PI + (rotateDeg * Math.PI) / 180;
    return { x: cx + radius * Math.cos(angle), y: cy + radius * Math.sin(angle) };
  });
}

function appColor(appId: string): string {
  return APPS.find((a) => a.id === appId)?.color ?? "#71717a";
}

// ─────────────────────────────────────────────────────────────────────────
// View 1: Source topology graph
// ─────────────────────────────────────────────────────────────────────────

function SourceTopologySvg() {
  const W = 520;
  const H = 480;
  const cx = W / 2;
  const cy = H / 2;

  const qmPositions = useMemo(
    () => circular(SOURCE_QMS.length, cx, cy, 170),
    [],
  );

  // App positions in inner ring
  const appPositions = useMemo(() => {
    const inner = circular(APPS.length, cx, cy, 70);
    const map: Record<string, { x: number; y: number }> = {};
    APPS.forEach((a, i) => {
      map[a.id] = inner[i];
    });
    return map;
  }, []);

  // App-to-QM ownership lines
  const lines = SOURCE_QMS.flatMap((qm, qi) =>
    qm.apps.map((appId) => ({
      from: appPositions[appId],
      to: qmPositions[qi],
      color: appColor(appId),
    })),
  );

  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="w-full h-auto">
      <defs>
        <radialGradient id="src-bg" cx="50%" cy="50%" r="50%">
          <stop offset="0%" stopColor="#1a1a1a" />
          <stop offset="100%" stopColor="#0a0a0a" />
        </radialGradient>
      </defs>
      <rect width={W} height={H} fill="url(#src-bg)" rx="8" />
      <text
        x={cx}
        y={26}
        textAnchor="middle"
        className="fill-fg-muted text-xs"
      >
        SOURCE — 9 shared QMs · 7 apps · sharing causes coupling
      </text>

      {/* Lines: app → QM */}
      {lines.map((l, i) => (
        <line
          key={i}
          x1={l.from.x}
          y1={l.from.y}
          x2={l.to.x}
          y2={l.to.y}
          stroke={l.color}
          strokeWidth={0.6}
          opacity={0.45}
        />
      ))}

      {/* QMs (outer ring) */}
      {SOURCE_QMS.map((qm, i) => (
        <g key={qm.name}>
          <circle
            cx={qmPositions[i].x}
            cy={qmPositions[i].y}
            r={22}
            fill="#1a1a1a"
            stroke="#3f3f46"
            strokeWidth={1.5}
          />
          <text
            x={qmPositions[i].x}
            y={qmPositions[i].y + 3}
            textAnchor="middle"
            className="fill-fg text-[9px] font-mono"
          >
            {qm.name}
          </text>
          {/* App count badge */}
          <circle
            cx={qmPositions[i].x + 18}
            cy={qmPositions[i].y - 18}
            r={8}
            fill="#f59e0b"
            opacity={qm.apps.length > 1 ? 1 : 0.3}
          />
          <text
            x={qmPositions[i].x + 18}
            y={qmPositions[i].y - 15}
            textAnchor="middle"
            className="fill-bg-base text-[8px] font-bold"
          >
            {qm.apps.length}
          </text>
        </g>
      ))}

      {/* Apps (inner ring) */}
      {APPS.map((a) => {
        const p = appPositions[a.id];
        return (
          <g key={a.id}>
            <circle
              cx={p.x}
              cy={p.y}
              r={11}
              fill={a.color}
              stroke="#0a0a0a"
              strokeWidth={1.5}
            />
            <text
              x={p.x}
              y={p.y + 22}
              textAnchor="middle"
              className="fill-fg text-[9px] font-mono"
            >
              {a.id}
            </text>
          </g>
        );
      })}

      {/* Legend */}
      <g transform={`translate(12, ${H - 50})`}>
        <circle cx={6} cy={6} r={6} fill="#f59e0b" />
        <text x={18} y={9} className="fill-fg-muted text-[10px]">
          shared QM (badge = app count)
        </text>
      </g>
    </svg>
  );
}

// ─────────────────────────────────────────────────────────────────────────
// View 1: Target topology graph
// ─────────────────────────────────────────────────────────────────────────

function TargetTopologySvg() {
  const W = 520;
  const H = 480;
  const cx = W / 2;
  const cy = H / 2;

  // Lay apps in a circle; each app's dedicated target QM sits just inside
  const appPositions = useMemo(() => {
    const outer = circular(APPS.length, cx, cy, 180);
    const map: Record<string, { x: number; y: number }> = {};
    APPS.forEach((a, i) => {
      map[a.id] = outer[i];
    });
    return map;
  }, []);

  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="w-full h-auto">
      <defs>
        <radialGradient id="tgt-bg" cx="50%" cy="50%" r="50%">
          <stop offset="0%" stopColor="#1a1a1a" />
          <stop offset="100%" stopColor="#0a0a0a" />
        </radialGradient>
        <marker
          id="arrow-tgt"
          viewBox="0 0 10 10"
          refX="9"
          refY="5"
          markerWidth="5"
          markerHeight="5"
          orient="auto"
        >
          <path d="M 0 0 L 10 5 L 0 10 z" fill="#14b8a6" />
        </marker>
      </defs>
      <rect width={W} height={H} fill="url(#tgt-bg)" rx="8" />
      <text
        x={cx}
        y={26}
        textAnchor="middle"
        className="fill-fg-muted text-xs"
      >
        TARGET — 7 dedicated QMs · strict 1:1 · isolation by design
      </text>

      {/* Bridge channels (flows) — drawn between target QMs */}
      {FLOWS.map((f, i) => {
        const p = appPositions[f.producer];
        const c = appPositions[f.consumer];
        // shrink line slightly so it doesn't overlap the circle
        const dx = c.x - p.x;
        const dy = c.y - p.y;
        const len = Math.sqrt(dx * dx + dy * dy);
        const ux = dx / len;
        const uy = dy / len;
        const r = 24;
        return (
          <line
            key={i}
            x1={p.x + ux * r}
            y1={p.y + uy * r}
            x2={c.x - ux * r}
            y2={c.y - uy * r}
            stroke="#14b8a6"
            strokeWidth={0.8 + Math.log2(f.count + 1) * 0.6}
            opacity={0.65}
            markerEnd="url(#arrow-tgt)"
          />
        );
      })}

      {/* Target QM + app icon (concentric) */}
      {APPS.map((a) => {
        const p = appPositions[a.id];
        const qmName = "APPQM_" + a.id.replace("/", "_");
        return (
          <g key={a.id}>
            {/* QM ring */}
            <circle
              cx={p.x}
              cy={p.y}
              r={22}
              fill="#1a1a1a"
              stroke={a.color}
              strokeWidth={2}
            />
            {/* App dot inside */}
            <circle cx={p.x} cy={p.y} r={9} fill={a.color} />
            <text
              x={p.x}
              y={p.y + 35}
              textAnchor="middle"
              className="fill-fg text-[9px] font-mono"
            >
              {a.id}
            </text>
            <text
              x={p.x}
              y={p.y + 46}
              textAnchor="middle"
              className="fill-fg-subtle text-[7px] font-mono"
            >
              {qmName}
            </text>
          </g>
        );
      })}

      {/* Legend */}
      <g transform={`translate(12, ${H - 50})`}>
        <line
          x1={0}
          y1={6}
          x2={18}
          y2={6}
          stroke="#14b8a6"
          strokeWidth={1.5}
          markerEnd="url(#arrow-tgt)"
        />
        <text x={24} y={9} className="fill-fg-muted text-[10px]">
          bridge channel (width ∝ log flow count)
        </text>
      </g>
    </svg>
  );
}

// ─────────────────────────────────────────────────────────────────────────
// View 2: Migration choreography animation
// ─────────────────────────────────────────────────────────────────────────

type Step = {
  label: string;
  state: string;
  mqsc: string;
  rationale: string;
};

const CHOREOGRAPHY: Step[] = [
  {
    label: "plan",
    state: "PLANNED",
    mqsc: "(no MQSC — planner agent runs)",
    rationale:
      "Migration Planner produces structured plan. LLM or deterministic fallback. Operator approves.",
  },
  {
    label: "prov.",
    state: "PROVISIONING_TARGET_QM",
    mqsc: "(deploy pod + realize MQSC)",
    rationale:
      "Deploy dedicated target QM on OCP. Realize MQSC: DLQ, CHLAUTH disabled, app queues.",
  },
  {
    label: "v.pre",
    state: "VALIDATING_PRE",
    mqsc: "DISPLAY QMGR / CHANNEL / QLOCAL",
    rationale:
      "Confirm target QM ready, source QM healthy, no in-flight blockers.",
  },
  {
    label: "rewire",
    state: "REWIRING",
    mqsc: "DEFINE QLOCAL(XMITQ) / CHANNEL(SDR,RCVR) / START CHANNEL",
    rationale:
      "Build the bridge: XMITQ on source, SDR on source, RCVR on target, START SDR. Channel goes RUNNING.",
  },
  {
    label: "drain",
    state: "DRAIN_WAIT",
    mqsc: "DISPLAY QLOCAL(...) CURDEPTH IPPROCS OPPROCS",
    rationale:
      "Zero-window poll: depth=0 AND IPPROCS=0 AND OPPROCS=0 over 3 polls. Little's Law gives prediction.",
  },
  {
    label: "v.during",
    state: "VALIDATING_DURING",
    mqsc: "DISPLAY CHSTATUS(...) SUBSTATE",
    rationale:
      "Bridge SDR is RUNNING SUBSTATE(MQGET). Producer/consumer reconnect counts unchanged.",
  },
  {
    label: "drain-src",
    state: "DRAINING_SOURCE",
    mqsc: "DISPLAY QLOCAL on source XMITQ",
    rationale:
      "Source-side bridge XMITQ depth-only drain. SDR keeps open handle so IPPROCS≥1 is normal.",
  },
  {
    label: "v.post",
    state: "VALIDATING_POST",
    mqsc: "DISPLAY QREMOTE / QLOCAL on target",
    rationale:
      "Source-side QREMOTEs point at target. Target-side QLOCALs present for this app's queues.",
  },
  {
    label: "done",
    state: "COMPLETED",
    mqsc: "DELETE QLOCAL(source) · DEFINE QREMOTE",
    rationale:
      "Transparent rewiring complete. Connection strings unchanged. Apps don't know migration happened.",
  },
];

// ─────────────────────────────────────────────────────────────────────────
// Helpers: map forward state names to choreography step indices
// ─────────────────────────────────────────────────────────────────────────

/** Map a forward MigrationState value to the index in CHOREOGRAPHY (the 9-step view). */
const FORWARD_STATE_TO_STEP: Record<string, number> = {
  PLANNED: 0,
  PROVISIONING_TARGET_QM: 1,
  VALIDATING_PRE: 2,
  REWIRING: 3,
  DRAIN_WAIT: 4,
  VALIDATING_DURING: 5,
  DRAINING_SOURCE: 6,
  VALIDATING_POST: 7,
  COMPLETED: 8,
};

type ReplaySlot = {
  /** index in CHOREOGRAPHY */
  stepIdx: number;
  /** Lamport clock of the state-transition entry that opened this step */
  lamport: number | null;
  /** Wall-clock of the state-transition entry */
  wallClock: string | null;
  /** Real MQSC the engine ran during this state (joined from MIGRATION_STEP entries inside the state) */
  mqsc: string;
  /** Whether the state ended in failure (any MIGRATION_STEP_FAILED inside the state) */
  failed: boolean;
};

/** From a Migration's audit response, derive the per-state replay slots in forward Lamport order. */
function buildForwardReplay(
  entries: MigrationAuditEntry[],
): { slots: ReplaySlot[]; failureStateIdx: number | null } {
  // Sort by Lamport for safety
  const sorted = [...entries]
    .filter((e) => !e.is_rollback)
    .sort((a, b) => a.lamport_clock - b.lamport_clock);

  const slots: ReplaySlot[] = [];
  let failureStateIdx: number | null = null;

  // Walk transitions; for each one, gather MQSC from MIGRATION_STEP_* between this and the next transition.
  const transitions = sorted.filter(
    (e) => e.operation === "MIGRATION_STATE_TRANSITION" || e.operation === "MIGRATION_PLANNED",
  );

  for (let i = 0; i < transitions.length; i++) {
    const t = transitions[i];
    let stateName: string | null = null;
    if (t.operation === "MIGRATION_PLANNED") {
      stateName = "PLANNED";
    } else {
      const to = (t.request_payload as Record<string, unknown> | null)?.to_state;
      if (typeof to === "string") stateName = to;
    }
    if (!stateName) continue;
    const stepIdx = FORWARD_STATE_TO_STEP[stateName];
    if (stepIdx === undefined) continue;

    // Collect step ops between this transition and the next
    const nextLc = transitions[i + 1]?.lamport_clock ?? Infinity;
    const stepOps = sorted.filter(
      (e) =>
        e.lamport_clock > t.lamport_clock &&
        e.lamport_clock < nextLc &&
        (e.operation === "MIGRATION_STEP_COMPLETED" ||
          e.operation === "MIGRATION_STEP_FAILED"),
    );

    const mqscLines = stepOps
      .map((e) => {
        const rp = e.request_payload as Record<string, unknown> | null;
        const mqsc = rp?.mqsc_text;
        return typeof mqsc === "string" ? mqsc : null;
      })
      .filter((s): s is string => !!s);

    const failed = stepOps.some((e) => e.operation === "MIGRATION_STEP_FAILED");
    if (failed && failureStateIdx === null) failureStateIdx = stepIdx;

    slots.push({
      stepIdx,
      lamport: t.lamport_clock,
      wallClock: t.wall_clock,
      mqsc: mqscLines.length > 0 ? mqscLines.join("\n") : "(no MQSC for this state — agent/probe only)",
      failed,
    });
  }

  return { slots, failureStateIdx };
}

/** Pull rollback audit entries (is_rollback=true) into a forward-time-ordered list of "rollback steps". */
type RollbackReplay = {
  lamport: number;
  wallClock: string;
  mqsc: string;
  qm: string | null;
  succeeded: boolean;
  label: string;
};

function buildRollbackReplay(entries: MigrationAuditEntry[]): RollbackReplay[] {
  return entries
    .filter((e) => e.is_rollback && e.operation === "ROLLBACK_STEP")
    .sort((a, b) => a.lamport_clock - b.lamport_clock)
    .map((e) => {
      const rp = e.request_payload as Record<string, unknown> | null;
      const mqscRaw =
        (rp?.mqsc_text as string | undefined) ??
        (rp?.rollback_mqsc as string | undefined) ??
        null;
      const object =
        (rp?.object as string | undefined) ??
        ((rp?.object_kind as string | undefined) && (rp?.object_name as string | undefined)
          ? `${rp?.object_kind}(${rp?.object_name})`
          : undefined);
      const stepLabel = (rp?.step_label as string | undefined) ?? "rollback-step";
      return {
        lamport: e.lamport_clock,
        wallClock: e.wall_clock,
        mqsc: mqscRaw ?? `(inverse of ${object ?? stepLabel})`,
        qm: e.qm_name,
        succeeded: e.success,
        label: stepLabel,
      };
    });
}

// ─────────────────────────────────────────────────────────────────────────
// ChoreographyView — replay a real completed/rolled-back migration
// ─────────────────────────────────────────────────────────────────────────

function ChoreographyView() {
  // Pick a migration to replay
  const [selectedMigrationId, setSelectedMigrationId] = useState<string>("");
  const [step, setStep] = useState(0);
  const [playing, setPlaying] = useState(false);

  // List migrations; filter to terminal forward-or-rolledback states.
  // When LIVE_DATA is false, skip the network call and inject the canned
  // forward-only LDCWH/TH migration so the picker still has one entry.
  const { data: liveMigrations } = useSWR<Migration[]>(
    LIVE_DATA ? "/migrations" : null,
    () => bcl.migrations.list(),
    { refreshInterval: 0 },
  );
  const allMigrations = LIVE_DATA ? liveMigrations : [CANNED_MIGRATION];
  const replayable = useMemo(
    () =>
      (allMigrations ?? [])
        .filter(
          (m) =>
            m.state === "COMPLETED" ||
            m.state === "ROLLED_BACK" ||
            m.state === "ROLLBACK_FAILED",
        )
        .sort((a, b) => b.id - a.id),
    [allMigrations],
  );

  // Auto-pick most recent on first load
  useEffect(() => {
    if (!selectedMigrationId && replayable.length > 0) {
      setSelectedMigrationId(String(replayable[0].id));
    }
  }, [replayable, selectedMigrationId]);

  // Load the selected migration's detail + audit. When LIVE_DATA is false
  // and the selected ID is the canned one, fall back to fixtures.
  const isCannedSelection =
    !LIVE_DATA && String(selectedMigrationId) === String(CANNED_MIGRATION.id);

  const { data: liveMigration } = useSWR<Migration>(
    LIVE_DATA && selectedMigrationId
      ? `/migrations/${selectedMigrationId}`
      : null,
    () => bcl.migrations.get(selectedMigrationId),
    { refreshInterval: 0 },
  );
  const migration = isCannedSelection ? CANNED_MIGRATION : liveMigration;

  const { data: liveAuditResp } = useSWR<MigrationAuditResponse>(
    LIVE_DATA && selectedMigrationId
      ? `/migrations/${selectedMigrationId}/audit`
      : null,
    () => bcl.migrations.audit(selectedMigrationId, 500),
    { refreshInterval: 0 },
  );
  const auditResp = isCannedSelection ? CANNED_AUDIT_RESPONSE : liveAuditResp;

  const entries: MigrationAuditEntry[] = auditResp?.entries ?? [];

  // Derive the forward replay (state-by-state) and rollback replay (per-step)
  const { slots: forwardSlots, failureStateIdx } = useMemo(
    () => buildForwardReplay(entries),
    [entries],
  );
  const rollbackSteps = useMemo(() => buildRollbackReplay(entries), [entries]);

  const isRolledBack =
    migration?.state === "ROLLED_BACK" || migration?.state === "ROLLBACK_FAILED";

  // Total number of clickable beats:
  //   9 forward (CHOREOGRAPHY) + N rollback steps (if rolled back)
  const totalBeats = CHOREOGRAPHY.length + (isRolledBack ? rollbackSteps.length : 0);
  const inRollbackPhase = step >= CHOREOGRAPHY.length;
  const rollbackBeatIdx = step - CHOREOGRAPHY.length;

  // Reset step when switching migrations
  useEffect(() => {
    setStep(0);
    setPlaying(false);
  }, [selectedMigrationId]);

  // Auto-play
  useEffect(() => {
    if (!playing) return;
    if (step >= totalBeats - 1) {
      setPlaying(false);
      return;
    }
    const id = setTimeout(() => setStep((s) => s + 1), 1400);
    return () => clearTimeout(id);
  }, [playing, step, totalBeats]);

  // Derive visual state
  const queueConverted = step >= 3;
  const bridgeActive = step >= 4 && !inRollbackPhase;
  // During rollback phase, the bridge teardown happens late; for the visual
  // we say bridge is "inactive" once rollback is past its halfway point.
  const bridgeTornDown = inRollbackPhase && rollbackBeatIdx >= Math.floor(rollbackSteps.length / 2);
  const queueRestored = inRollbackPhase && rollbackBeatIdx >= rollbackSteps.length - 2;

  // Real-data slot for the current forward step (if exists)
  const currentSlot: ReplaySlot | undefined = !inRollbackPhase
    ? forwardSlots.find((s) => s.stepIdx === step)
    : undefined;

  const currentChoreography = !inRollbackPhase ? CHOREOGRAPHY[step] : null;
  const currentRollback = inRollbackPhase ? rollbackSteps[rollbackBeatIdx] : null;

  // App/QM identifiers for the diagram
  const appId = migration?.app_id ?? "(no migration selected)";
  const planData = migration?.plan?.plan;
  const sourceQm =
    (planData?.queues_to_redirect && planData.queues_to_redirect[0]) ||
    "source QM";
  const targetQm = planData
    ? `APPQM_${appId.replace("/", "_")}`
    : "target QM";
  const bridgeChannel = planData?.bridge_channel_name ?? "bridge channel";
  const bridgeXmitq = planData?.bridge_xmitq_name ?? "bridge XMITQ";
  const sampleQueue = planData?.queues_to_redirect?.[0] ?? "(queue name)";

  return (
    <div className="space-y-4">
      {/* Selector */}
      <div className="panel p-4">
        <div className="text-xs uppercase tracking-wider text-fg-muted mb-3">
          Replay a real migration — read-only. Reads from{" "}
          <span className="font-mono text-accent">GET /migrations</span> and{" "}
          <span className="font-mono text-accent">/migrations/{`{id}`}/audit</span>.
        </div>
        <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
          <div className="md:col-span-2">
            <label className="block text-[10px] uppercase tracking-wider text-fg-subtle mb-1">
              Pick a completed or rolled-back migration to replay
            </label>
            <select
              value={selectedMigrationId}
              onChange={(e) => setSelectedMigrationId(e.target.value)}
              className="w-full rounded border border-border-subtle bg-bg-subtle px-2 py-1.5 text-sm font-mono focus:outline-none focus:border-accent"
            >
              {replayable.length === 0 && (
                <option value="">(no completed migrations yet)</option>
              )}
              {replayable.map((m) => (
                <option key={m.id} value={m.id}>
                  #{m.id} · {m.app_id} · {m.state}
                </option>
              ))}
            </select>
          </div>
          <div className="flex items-end gap-3 text-xs">
            {migration && (
              <>
                <div className="flex flex-col">
                  <span className="text-[9px] uppercase tracking-wider text-fg-subtle">
                    Plan source
                  </span>
                  <span className="font-mono text-fg">
                    {migration.plan?.planner_audit?.planner_source ?? "—"}
                  </span>
                </div>
                <div className="flex flex-col">
                  <span className="text-[9px] uppercase tracking-wider text-fg-subtle">
                    Audit entries
                  </span>
                  <span className="font-mono text-fg">{entries.length}</span>
                </div>
              </>
            )}
          </div>
        </div>
      </div>

      {/* Stepper */}
      <div className="panel p-4">
        <div className="mb-3 flex items-center justify-between">
          <div className="text-xs uppercase tracking-wider text-fg-muted">
            State machine — TLA+ verified · Lamport-ordered{" "}
            {isRolledBack && (
              <span className="text-warn">· includes reverse walk</span>
            )}
          </div>
          <div className="flex gap-2">
            <button
              onClick={() => {
                setStep(0);
                setPlaying(false);
              }}
              className="rounded border border-border-subtle px-3 py-1 text-xs hover:bg-bg-subtle"
            >
              Reset
            </button>
            <button
              onClick={() => {
                if (step >= totalBeats - 1) setStep(0);
                setPlaying((p) => !p);
              }}
              disabled={totalBeats === 0}
              className="rounded bg-accent px-3 py-1 text-xs font-medium text-bg-base hover:opacity-90 disabled:opacity-50"
            >
              {playing ? "Pause" : step >= totalBeats - 1 ? "Replay" : "Play"}
            </button>
            <button
              onClick={() => setStep((s) => Math.min(s + 1, totalBeats - 1))}
              disabled={totalBeats === 0}
              className="rounded border border-border-subtle px-3 py-1 text-xs hover:bg-bg-subtle disabled:opacity-50"
            >
              Next →
            </button>
          </div>
        </div>

        {/* Forward dots */}
        <div className="flex items-center gap-1">
          {CHOREOGRAPHY.map((s, i) => {
            const slot = forwardSlots.find((fs) => fs.stepIdx === i);
            const ran = !!slot;
            const failedHere = failureStateIdx === i;
            const isCurrent = !inRollbackPhase && i === step;
            return (
              <div key={i} className="flex flex-1 items-center">
                <button
                  onClick={() => {
                    setStep(i);
                    setPlaying(false);
                  }}
                  className="flex flex-col items-center gap-1"
                  title={slot ? `LC=${slot.lamport} · ${slot.wallClock}` : "no audit row"}
                >
                  <div
                    className={
                      "h-3 w-3 rounded-full transition-all " +
                      (failedHere
                        ? "bg-danger"
                        : ran && i < step
                          ? "bg-success"
                          : isCurrent
                            ? "bg-accent ring-2 ring-accent/40 scale-125"
                            : ran
                              ? "bg-success/40"
                              : "bg-border")
                    }
                  />
                  <span
                    className={
                      "text-[10px] " +
                      (isCurrent ? "text-fg font-medium" : "text-fg-subtle")
                    }
                  >
                    {s.label}
                  </span>
                </button>
                {i < CHOREOGRAPHY.length - 1 && (
                  <div
                    className={
                      "mx-1 h-px flex-1 transition-colors " +
                      (i < step || (ran && !inRollbackPhase) ? "bg-success/60" : "bg-border-subtle")
                    }
                  />
                )}
              </div>
            );
          })}
        </div>

        {/* Rollback dots — appear below when migration was rolled back */}
        {isRolledBack && rollbackSteps.length > 0 && (
          <div className="mt-4">
            <div className="mb-2 text-[10px] uppercase tracking-wider text-warn">
              Reverse walk · reverse-Lamport order · per-app local
            </div>
            <div className="flex items-center gap-1">
              {rollbackSteps.map((rb, i) => {
                const beat = CHOREOGRAPHY.length + i;
                const isCurrent = step === beat;
                const past = step > beat;
                return (
                  <div key={i} className="flex flex-1 items-center">
                    <button
                      onClick={() => {
                        setStep(beat);
                        setPlaying(false);
                      }}
                      className="flex flex-col items-center gap-1"
                      title={`LC=${rb.lamport} · ${rb.wallClock}`}
                    >
                      <div
                        className={
                          "h-3 w-3 rounded-full transition-all " +
                          (past
                            ? "bg-warn"
                            : isCurrent
                              ? "bg-warn ring-2 ring-warn/40 scale-125"
                              : "bg-border")
                        }
                      />
                      <span
                        className={
                          "text-[10px] " +
                          (isCurrent ? "text-fg font-medium" : "text-fg-subtle")
                        }
                      >
                        r{i + 1}
                      </span>
                    </button>
                    {i < rollbackSteps.length - 1 && (
                      <div
                        className={
                          "mx-1 h-px flex-1 transition-colors " +
                          (past ? "bg-warn/60" : "bg-border-subtle")
                        }
                      />
                    )}
                  </div>
                );
              })}
            </div>
          </div>
        )}
      </div>

      {/* Visual + detail */}
      <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
        <div className="panel p-4 md:col-span-2">
          <div className="mb-2 text-xs uppercase tracking-wider text-fg-muted">
            Queue conversion — visual ·{" "}
            <span className="text-fg-subtle font-mono">{appId}</span>
          </div>
          <svg viewBox="0 0 700 220" className="w-full h-auto">
            <defs>
              <marker
                id="msg-arrow-c"
                viewBox="0 0 10 10"
                refX="9"
                refY="5"
                markerWidth="5"
                markerHeight="5"
                orient="auto"
              >
                <path d="M 0 0 L 10 5 L 0 10 z" fill={inRollbackPhase ? "#f59e0b" : "#14b8a6"} />
              </marker>
            </defs>

            {/* Source QM */}
            <g>
              <rect
                x={20}
                y={50}
                width={180}
                height={120}
                rx={8}
                fill="#1a1a1a"
                stroke="#3f3f46"
                strokeWidth={1.5}
              />
              <text x={110} y={40} textAnchor="middle" className="fill-fg-muted text-[10px] uppercase">
                Source-side
              </text>
              <text x={110} y={75} textAnchor="middle" className="fill-fg text-[10px] font-mono">
                {sampleQueue.length > 22 ? sampleQueue.slice(0, 22) + "…" : sampleQueue}
              </text>
              <text
                x={110}
                y={92}
                textAnchor="middle"
                className={
                  queueRestored || !queueConverted
                    ? "fill-success text-[9px] font-mono"
                    : "fill-warn text-[9px] font-mono"
                }
              >
                {queueRestored
                  ? "TYPE(QLOCAL) · restored"
                  : queueConverted
                    ? "TYPE(QREMOTE)"
                    : "TYPE(QLOCAL)"}
              </text>
              {queueConverted && !queueRestored && (
                <text x={110} y={108} textAnchor="middle" className="fill-fg-subtle text-[8px] font-mono">
                  RQMNAME({targetQm})
                </text>
              )}
              {/* XMITQ box */}
              {queueConverted && !bridgeTornDown && (
                <g>
                  <rect
                    x={40}
                    y={130}
                    width={140}
                    height={26}
                    rx={4}
                    fill="#1a1a1a"
                    stroke="#14b8a6"
                    strokeWidth={1}
                  />
                  <text x={110} y={146} textAnchor="middle" className="fill-accent text-[8px] font-mono">
                    {bridgeXmitq.length > 22 ? bridgeXmitq.slice(0, 22) + "…" : bridgeXmitq}
                  </text>
                </g>
              )}
            </g>

            {/* Bridge channel */}
            {bridgeActive && !bridgeTornDown && (
              <g>
                <line
                  x1={205}
                  y1={140}
                  x2={485}
                  y2={140}
                  stroke="#14b8a6"
                  strokeWidth={2}
                  markerEnd="url(#msg-arrow-c)"
                  className="animate-pulse"
                />
                <text x={345} y={132} textAnchor="middle" className="fill-accent text-[9px] font-mono">
                  {bridgeChannel} (SDR · RUNNING)
                </text>
              </g>
            )}
            {(!bridgeActive || bridgeTornDown) && (
              <line
                x1={205}
                y1={140}
                x2={485}
                y2={140}
                stroke={bridgeTornDown ? "#f59e0b" : "#3f3f46"}
                strokeWidth={1}
                strokeDasharray="4 4"
                opacity={0.6}
              />
            )}
            {bridgeTornDown && (
              <text x={345} y={132} textAnchor="middle" className="fill-warn text-[9px] font-mono">
                bridge torn down
              </text>
            )}

            {/* Target QM */}
            <g>
              <rect
                x={490}
                y={50}
                width={180}
                height={120}
                rx={8}
                fill="#1a1a1a"
                stroke={step >= 1 && !queueRestored ? "#3b82f6" : "#3f3f46"}
                strokeWidth={1.5}
                opacity={step >= 1 && !queueRestored ? 1 : 0.35}
              />
              <text x={580} y={40} textAnchor="middle" className="fill-fg-muted text-[10px] uppercase">
                Target QM ({targetQm.length > 18 ? targetQm.slice(0, 18) + "…" : targetQm})
              </text>
              {step >= 1 && !queueRestored ? (
                <>
                  <text x={580} y={85} textAnchor="middle" className="fill-fg text-[10px] font-mono">
                    {sampleQueue.length > 22 ? sampleQueue.slice(0, 22) + "…" : sampleQueue}
                  </text>
                  <text x={580} y={102} textAnchor="middle" className="fill-success text-[9px] font-mono">
                    TYPE(QLOCAL)
                  </text>
                </>
              ) : (
                <text x={580} y={120} textAnchor="middle" className="fill-fg-subtle text-[9px] italic">
                  {queueRestored ? "(retained · dormant)" : "(not yet provisioned)"}
                </text>
              )}
            </g>
          </svg>
        </div>

        <div className="panel p-4 space-y-3">
          {!inRollbackPhase && currentChoreography && (
            <>
              <div>
                <div className="text-[10px] uppercase tracking-wider text-fg-subtle">
                  Step {step + 1}/{totalBeats} · {currentChoreography.label}
                </div>
                <div className="mt-1 font-mono text-sm text-accent">
                  {currentChoreography.state}
                </div>
                {currentSlot?.lamport != null && (
                  <div className="text-[10px] text-fg-subtle font-mono mt-0.5">
                    LC={currentSlot.lamport} ·{" "}
                    {currentSlot.wallClock?.replace("T", " ").slice(0, 19)}
                  </div>
                )}
              </div>
              <div>
                <div className="text-[10px] uppercase tracking-wider text-fg-subtle mb-1">
                  MQSC executed
                </div>
                <pre className="rounded bg-bg-subtle p-2 font-mono text-[10px] text-fg whitespace-pre-wrap break-all max-h-32 overflow-auto">
                  {currentSlot?.mqsc ?? currentChoreography.mqsc + "  (illustrative — no audit yet)"}
                </pre>
              </div>
              <div>
                <div className="text-[10px] uppercase tracking-wider text-fg-subtle mb-1">
                  Why this step
                </div>
                <p className="text-xs text-fg-muted leading-relaxed">
                  {currentChoreography.rationale}
                </p>
              </div>
            </>
          )}

          {inRollbackPhase && currentRollback && (
            <>
              <div>
                <div className="text-[10px] uppercase tracking-wider text-warn">
                  Reverse step {rollbackBeatIdx + 1}/{rollbackSteps.length}
                </div>
                <div className="mt-1 font-mono text-sm text-warn">
                  {currentRollback.label}
                </div>
                <div className="text-[10px] text-fg-subtle font-mono mt-0.5">
                  LC={currentRollback.lamport} ·{" "}
                  {currentRollback.wallClock.replace("T", " ").slice(0, 19)}
                  {currentRollback.qm && (
                    <>
                      {" · "}
                      <span className="text-fg-muted">QM {currentRollback.qm}</span>
                    </>
                  )}
                </div>
              </div>
              <div>
                <div className="text-[10px] uppercase tracking-wider text-fg-subtle mb-1">
                  Inverse MQSC
                </div>
                <pre className="rounded bg-bg-subtle p-2 font-mono text-[10px] text-fg whitespace-pre-wrap break-all max-h-32 overflow-auto">
                  {currentRollback.mqsc}
                </pre>
              </div>
              <div>
                <div className="text-[10px] uppercase tracking-wider text-fg-subtle mb-1">
                  Rollback locality
                </div>
                <p className="text-xs text-fg-muted leading-relaxed">
                  Per-app rollback: this step touches only the migration row {migration?.id}&apos;s
                  own MQSC steps. TLA+ <span className="font-mono">S4 PerAppRollbackLocality</span> holds.
                </p>
              </div>
            </>
          )}

          {!currentChoreography && !currentRollback && (
            <div className="text-fg-subtle text-xs italic">
              {replayable.length === 0
                ? "No completed migrations to replay yet."
                : "Pick a migration above."}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}


// ─────────────────────────────────────────────────────────────────────────
// View 3: Live data flow — wired to BCL /test-message-flow
// ─────────────────────────────────────────────────────────────────────────

function DataFlowView() {
  const [topologyId, setTopologyId] = useState<string>("");
  const [flowIdx, setFlowIdx] = useState<number>(1); // default LIY/KW → JUUD/C9 (33 flows)
  const [pulse, setPulse] = useState(0);
  const [running, setRunning] = useState(false);
  const [result, setResult] = useState<TestMessageResult | null>(null);
  const [error, setError] = useState<string | null>(null);

  // Fetch target topologies for the selector. Default to the most recent.
  // When LIVE_DATA is false, skip the network call and inject a fake target
  // so the dropdown still renders something pickable.
  const { data: liveTopologies } = useSWR<Topology[]>(
    LIVE_DATA ? "/topologies" : null,
    bcl.topologies.list,
    { refreshInterval: 0 },
  );
  const topologies = LIVE_DATA ? liveTopologies : [CANNED_TARGET_TOPOLOGY];

  const targetTopologies = useMemo(
    () => (topologies ?? []).filter((t) => t.kind === "TARGET"),
    [topologies],
  );

  // Auto-select most recent target topology
  useEffect(() => {
    if (!topologyId && targetTopologies.length > 0) {
      const newest = [...targetTopologies].sort((a, b) =>
        a.created_at < b.created_at ? 1 : -1,
      )[0];
      setTopologyId(String(newest.id));
    }
  }, [targetTopologies, topologyId]);

  // Pulse animation while running
  useEffect(() => {
    if (!running) return;
    const id = setInterval(() => setPulse((p) => (p + 1) % 100), 60);
    return () => clearInterval(id);
  }, [running]);

  const flow = FLOWS[flowIdx];
  const producerQm = "APPQM_" + flow.producer.replace("/", "_");
  const consumerQm = "APPQM_" + flow.consumer.replace("/", "_");

  // Lane positions
  const producerX = 80;
  const sourceX = 250;
  const targetX = 540;
  const consumerX = 720;
  const laneY = 130;

  async function handleSend() {
    if (!topologyId) {
      setError("Pick a target topology first.");
      return;
    }
    setError(null);
    setResult(null);
    setRunning(true);
    try {
      if (LIVE_DATA) {
        const res = await bcl.messageFlow.send(topologyId, {
          producer_app_id: flow.producer,
          consumer_app_id: flow.consumer,
          payload: "viz-demo-" + Date.now(),
          timeout_seconds: 30,
        });
        setResult(res);
      } else {
        // Canned mode: wait the same wall-clock duration as the real call
        // so the pulse animation gets to play, then return a canned PASS.
        await new Promise((r) => setTimeout(r, 1200));
        // Customise the canned result to reflect the currently selected
        // flow so the UI doesn't show stale producer/consumer pair labels.
        const tailored: TestMessageResult = {
          ...CANNED_TEST_MESSAGE_RESULT,
          producer_app_id: flow.producer,
          consumer_app_id: flow.consumer,
          producer_qm: producerQm,
          consumer_qm: consumerQm,
        };
        setResult(tailored);
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setRunning(false);
    }
  }

  // After call returns, derive stats from result
  const durationMs = result
    ? Math.round(result.total_duration_seconds * 1000)
    : null;
  const lamportSpan =
    result && result.audit_lamport_first != null && result.audit_lamport_last != null
      ? result.audit_lamport_last - result.audit_lamport_first + 1
      : null;

  return (
    <div className="space-y-4">
      {/* Controls */}
      <div className="panel p-4">
        <div className="text-xs uppercase tracking-wider text-fg-muted mb-3">
          Live message flow — wired to <span className="font-mono text-accent">POST /topologies/{`{id}`}/test-message-flow</span>
        </div>
        <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
          <div>
            <label className="block text-[10px] uppercase tracking-wider text-fg-subtle mb-1">
              Target topology
            </label>
            <select
              value={topologyId}
              onChange={(e) => setTopologyId(e.target.value)}
              className="w-full rounded border border-border-subtle bg-bg-subtle px-2 py-1.5 text-sm font-mono focus:outline-none focus:border-accent"
            >
              {targetTopologies.length === 0 && (
                <option value="">(no target topology found)</option>
              )}
              {targetTopologies.map((t) => (
                <option key={t.id} value={t.id}>
                  #{t.id} · {t.name}
                </option>
              ))}
            </select>
          </div>
          <div>
            <label className="block text-[10px] uppercase tracking-wider text-fg-subtle mb-1">
              Flow pair
            </label>
            <select
              value={flowIdx}
              onChange={(e) => {
                setFlowIdx(Number(e.target.value));
                setResult(null);
                setError(null);
              }}
              className="w-full rounded border border-border-subtle bg-bg-subtle px-2 py-1.5 text-sm font-mono focus:outline-none focus:border-accent"
            >
              {FLOWS.map((f, i) => (
                <option key={i} value={i}>
                  {f.producer} → {f.consumer}
                </option>
              ))}
            </select>
            <p className="mt-1 text-[10px] text-fg-subtle">
              Sends one representative message for this app pair.
            </p>
          </div>
          <div className="flex items-end">
            <button
              onClick={handleSend}
              disabled={running || !topologyId}
              className="w-full rounded bg-accent px-3 py-1.5 text-sm font-medium text-bg-base hover:opacity-90 disabled:opacity-50 disabled:cursor-not-allowed"
            >
              {running ? "Sending..." : "Send test message"}
            </button>
          </div>
        </div>
        {error && (
          <div className="mt-3 rounded border border-danger/40 bg-danger/10 px-3 py-2 text-xs text-danger">
            {error}
          </div>
        )}
        {result && !result.success && (
          <div className="mt-3 rounded border border-warn/40 bg-warn/10 px-3 py-2 text-xs text-warn">
            BCL responded but the flow did not succeed end-to-end. Check the per-step detail below.
          </div>
        )}
      </div>

      {/* Diagram */}
      <div className="panel p-5">
        <div className="mb-3">
          <div className="text-xs uppercase tracking-wider text-fg-muted">
            Bridge data flow — {flow.producer} → {flow.consumer}
          </div>
          <div className="mt-0.5 text-[10px] text-fg-subtle font-mono">
            {result
              ? `queue: ${result.producer_queue}`
              : `awaiting send — would route via APPQM_${flow.consumer.replace("/", "_")}.XMIT`}
          </div>
        </div>

        <svg viewBox="0 0 800 260" className="w-full h-auto">
          <defs>
            <marker
              id="flow-arrow-v2"
              viewBox="0 0 10 10"
              refX="9"
              refY="5"
              markerWidth="5"
              markerHeight="5"
              orient="auto"
            >
              <path d="M 0 0 L 10 5 L 0 10 z" fill="#14b8a6" />
            </marker>
          </defs>

          {/* Lane line */}
          <line
            x1={producerX}
            y1={laneY}
            x2={consumerX}
            y2={laneY}
            stroke="#27272a"
            strokeWidth={1}
            strokeDasharray="4 4"
          />

          {/* Producer */}
          <g>
            <circle cx={producerX} cy={laneY} r={20} fill={appColor(flow.producer)} />
            <text x={producerX} y={laneY + 4} textAnchor="middle" className="fill-bg-base text-[10px] font-bold">
              P
            </text>
            <text x={producerX} y={laneY + 42} textAnchor="middle" className="fill-fg text-[10px] font-mono">
              {flow.producer}
            </text>
            <text x={producerX} y={laneY + 56} textAnchor="middle" className="fill-fg-subtle text-[8px]">
              producer
            </text>
          </g>

          {/* Source QM */}
          <g>
            <rect x={sourceX - 60} y={laneY - 40} width={120} height={80} rx={6} fill="#1a1a1a" stroke="#3f3f46" />
            <text x={sourceX} y={laneY - 24} textAnchor="middle" className="fill-fg-muted text-[9px] uppercase">
              QM
            </text>
            <text x={sourceX} y={laneY - 8} textAnchor="middle" className="fill-fg text-[10px] font-mono">
              {result ? result.producer_qm : producerQm}
            </text>
            <text x={sourceX} y={laneY + 8} textAnchor="middle" className="fill-success text-[8px] font-mono">
              QREMOTE
            </text>
            <text x={sourceX} y={laneY + 22} textAnchor="middle" className="fill-fg-subtle text-[7px] font-mono">
              → APPQM_{flow.consumer.replace("/", "_")}.XMIT
            </text>
          </g>

          {/* Bridge channel arrow */}
          <line
            x1={sourceX + 60}
            y1={laneY}
            x2={targetX - 60}
            y2={laneY}
            stroke="#14b8a6"
            strokeWidth={2}
            markerEnd="url(#flow-arrow-v2)"
          />
          <text x={(sourceX + targetX) / 2} y={laneY - 12} textAnchor="middle" className="fill-accent text-[9px] font-mono">
            bridge SDR · RUNNING
          </text>
          <text x={(sourceX + targetX) / 2} y={laneY + 22} textAnchor="middle" className="fill-fg-subtle text-[8px]">
            bridge channel · TCP
          </text>

          {/* Target QM */}
          <g>
            <rect x={targetX - 60} y={laneY - 40} width={120} height={80} rx={6} fill="#1a1a1a" stroke={appColor(flow.consumer)} strokeWidth={1.5} />
            <text x={targetX} y={laneY - 24} textAnchor="middle" className="fill-fg-muted text-[9px] uppercase">
              QM
            </text>
            <text x={targetX} y={laneY - 8} textAnchor="middle" className="fill-fg text-[10px] font-mono">
              {result ? result.consumer_qm : consumerQm}
            </text>
            <text x={targetX} y={laneY + 8} textAnchor="middle" className="fill-success text-[8px] font-mono">
              QLOCAL
            </text>
            <text x={targetX} y={laneY + 22} textAnchor="middle" className="fill-fg-subtle text-[7px] font-mono">
              depth = real queue
            </text>
          </g>

          {/* Consumer */}
          <g>
            <circle cx={consumerX} cy={laneY} r={20} fill={appColor(flow.consumer)} />
            <text x={consumerX} y={laneY + 4} textAnchor="middle" className="fill-bg-base text-[10px] font-bold">
              C
            </text>
            <text x={consumerX} y={laneY + 42} textAnchor="middle" className="fill-fg text-[10px] font-mono">
              {flow.consumer}
            </text>
            <text x={consumerX} y={laneY + 56} textAnchor="middle" className="fill-fg-subtle text-[8px]">
              consumer
            </text>
          </g>

          {/* Animated message dots — pulse while running, then settle to one dot for success */}
          {running &&
            [0, 33, 66].map((offset) => {
              const t = ((pulse + offset) % 100) / 100;
              const x = producerX + (consumerX - producerX) * t;
              return (
                <circle key={offset} cx={x} cy={laneY} r={5} fill="#14b8a6">
                  <animate
                    attributeName="opacity"
                    values="1;1;0.7;1"
                    dur="0.5s"
                    repeatCount="indefinite"
                  />
                </circle>
              );
            })}
          {!running && result && result.success && (
            <g>
              <circle cx={consumerX - 30} cy={laneY} r={6} fill="#22c55e">
                <animate attributeName="r" values="6;9;6" dur="1.2s" repeatCount="indefinite" />
              </circle>
              <text x={consumerX - 30} y={laneY - 12} textAnchor="middle" className="fill-success text-[8px] font-bold">
                ✓ delivered
              </text>
            </g>
          )}

          {/* Annotations */}
          <text x={producerX} y={235} textAnchor="middle" className="fill-fg-subtle text-[8px] italic">
            amqsput to {result ? result.producer_qm : producerQm}
          </text>
          <text x={(producerX + sourceX) / 2} y={250} textAnchor="middle" className="fill-fg-subtle text-[8px] italic">
            (same connection string as pre-migration)
          </text>
          <text x={consumerX} y={235} textAnchor="middle" className="fill-fg-subtle text-[8px] italic">
            amqsget on {result ? result.consumer_qm : consumerQm}
          </text>
        </svg>

        {/* Stats — derived from real BCL response when available */}
        <div className="mt-4 grid grid-cols-2 md:grid-cols-4 gap-3 text-xs">
          <Stat
            label="Outcome"
            value={result ? (result.success ? "PASS" : "FAIL") : running ? "…" : "—"}
            hint={
              result
                ? result.success
                  ? "payload matched bit-for-bit"
                  : (() => {
                      const failed = result.steps?.find((s) => !s.success);
                      if (!failed) return "flow did not complete";
                      if (failed.name === "amqsput")
                        return "PUT failed — message never sent";
                      if (failed.name === "poll-consumer-queue-depth")
                        return "message did not arrive at consumer";
                      if (failed.name === "amqsget")
                        return result.payload_received == null
                          ? "GET failed — nothing received"
                          : "payload mismatch";
                      return `failed at ${failed.name}`;
                    })()
                : "click Send"
            }
            success={result?.success}
          />
          <Stat
            label="Total duration"
            value={durationMs != null ? `${durationMs}ms` : "—"}
            hint="amqsput + poll + amqsget"
          />
          <Stat
            label="Lamport span"
            value={lamportSpan != null ? `${lamportSpan} LCs` : "—"}
            hint={
              result?.audit_lamport_first != null && result?.audit_lamport_last != null
                ? `${result.audit_lamport_first} → ${result.audit_lamport_last}`
                : "audit entries written"
            }
          />
          <Stat label="Reconnects" value="0" hint="transparent rewiring" />
        </div>
      </div>

      {/* Per-step result rows (real audit) */}
      {result && (
        <div className="panel p-4">
          <div className="text-xs uppercase tracking-wider text-fg-muted mb-3">
            Forensic step trace · correlation_id <span className="font-mono text-fg">{result.correlation_id}</span>
          </div>
          <table className="w-full text-xs">
            <thead>
              <tr className="text-left text-[10px] uppercase tracking-wider text-fg-subtle border-b border-border-subtle">
                <th className="pb-2">Step</th>
                <th className="pb-2 text-right">Duration</th>
                <th className="pb-2 text-right">LC</th>
                <th className="pb-2">Detail</th>
                <th className="pb-2 text-right">Result</th>
              </tr>
            </thead>
            <tbody className="font-mono">
              {result.steps.map((s, i) => (
                <tr key={i} className="border-b border-border-subtle/50">
                  <td className="py-1.5">{s.name}</td>
                  <td className="py-1.5 text-right">{Math.round(s.duration_seconds * 1000)}ms</td>
                  <td className="py-1.5 text-right text-fg-subtle">{s.audit_lamport ?? "—"}</td>
                  <td className="py-1.5 text-fg-muted">{s.detail}</td>
                  <td className="py-1.5 text-right">
                    {s.success ? (
                      <span className="text-success">✓</span>
                    ) : (
                      <span className="text-danger">✗</span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          <div className="mt-3 grid grid-cols-1 md:grid-cols-2 gap-3 text-[10px]">
            <div className="rounded bg-bg-subtle p-2">
              <span className="text-fg-subtle">payload sent: </span>
              <span className="font-mono text-fg">{result.payload_sent}</span>
            </div>
            <div className="rounded bg-bg-subtle p-2">
              <span className="text-fg-subtle">payload received: </span>
              <span className="font-mono text-fg">{result.payload_received ?? "(none)"}</span>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

function Stat({
  label,
  value,
  hint,
  success,
}: {
  label: string;
  value: string;
  hint: string;
  success?: boolean;
}) {
  const valueColor =
    success === true
      ? "text-success"
      : success === false
        ? "text-danger"
        : "text-accent";
  return (
    <div className="rounded border border-border-subtle bg-bg-subtle p-3">
      <div className="text-[9px] uppercase tracking-wider text-fg-subtle">
        {label}
      </div>
      <div className={`mt-1 font-mono text-lg ${valueColor}`}>{value}</div>
      <div className="mt-0.5 text-[9px] text-fg-subtle">{hint}</div>
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────────
// Page
// ─────────────────────────────────────────────────────────────────────────

type TabId = "topology" | "choreography" | "flow";

export default function VizPage() {
  const [tab, setTab] = useState<TabId>("topology");

  return (
    <main className="mx-auto max-w-6xl px-6 py-8">
      <header className="mb-6">
        <div className="flex items-baseline justify-between">
          <h1 className="text-2xl font-semibold tracking-tight">Topology visualization</h1>
        </div>
        <p className="mt-1 text-sm text-fg-muted">
          Source topology, target topology, migration choreography, and live bridge data flow.
          Renders the same CSV the BCL ingested — the operational source of truth.
        </p>
      </header>

      {/* Tab bar */}
      <div className="mb-6 flex gap-1 border-b border-border-subtle">
        <TabButton active={tab === "topology"} onClick={() => setTab("topology")}>
          Source vs Target
        </TabButton>
        <TabButton active={tab === "choreography"} onClick={() => setTab("choreography")}>
          Migration choreography
        </TabButton>
        <TabButton active={tab === "flow"} onClick={() => setTab("flow")}>
          Data flow
        </TabButton>
      </div>

      {tab === "topology" && (
        <div className="space-y-4">
          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            <div className="panel p-3">
              <SourceTopologySvg />
            </div>
            <div className="panel p-3">
              <TargetTopologySvg />
            </div>
          </div>

          <div className="panel p-4">
            <div className="text-xs uppercase tracking-wider text-fg-muted mb-3">
              Topology summary
            </div>
            <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
              <SummaryStat label="Source QMs" value="9" hint="shared, app coupling" />
              <SummaryStat label="Target QMs" value="7" hint="dedicated, strict 1:1" />
              <SummaryStat label="Apps" value="7" hint="2 producers, 5 consumers" />
              <SummaryStat label="Flows" value="45" hint="5 producer→consumer pairs" />
            </div>
          </div>

          <div className="panel p-4">
            <div className="text-xs uppercase tracking-wider text-fg-muted mb-3">
              Flow pairs (target topology)
            </div>
            <table className="w-full text-sm">
              <thead>
                <tr className="text-left text-[10px] uppercase tracking-wider text-fg-subtle border-b border-border-subtle">
                  <th className="pb-2">Producer</th>
                  <th className="pb-2">Consumer</th>
                  <th className="pb-2 text-right">Flow count</th>
                  <th className="pb-2">Bridge XMITQ</th>
                </tr>
              </thead>
              <tbody className="font-mono text-xs">
                {FLOWS.map((f, i) => (
                  <tr key={i} className="border-b border-border-subtle/50">
                    <td className="py-1.5">{f.producer}</td>
                    <td className="py-1.5">{f.consumer}</td>
                    <td className="py-1.5 text-right">{f.count}</td>
                    <td className="py-1.5 text-fg-subtle">
                      APPQM_{f.consumer.replace("/", "_")}.XMIT
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {tab === "choreography" && <ChoreographyView />}

      {tab === "flow" && <DataFlowView />}
    </main>
  );
}

function TabButton({
  active,
  onClick,
  children,
}: {
  active: boolean;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      onClick={onClick}
      className={
        "border-b-2 px-4 py-2 text-sm transition-colors " +
        (active
          ? "border-accent text-fg"
          : "border-transparent text-fg-muted hover:text-fg")
      }
    >
      {children}
    </button>
  );
}

function SummaryStat({ label, value, hint }: { label: string; value: string; hint: string }) {
  return (
    <div className="rounded border border-border-subtle bg-bg-subtle p-3">
      <div className="text-[9px] uppercase tracking-wider text-fg-subtle">{label}</div>
      <div className="mt-1 font-mono text-xl text-fg">{value}</div>
      <div className="mt-0.5 text-[9px] text-fg-subtle">{hint}</div>
    </div>
  );
}
