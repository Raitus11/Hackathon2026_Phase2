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

function ChoreographyView() {
  const [step, setStep] = useState(0);
  const [playing, setPlaying] = useState(false);

  useEffect(() => {
    if (!playing) return;
    if (step >= CHOREOGRAPHY.length - 1) {
      setPlaying(false);
      return;
    }
    const id = setTimeout(() => setStep((s) => s + 1), 1800);
    return () => clearTimeout(id);
  }, [playing, step]);

  // Visual state: queue conversion happens at rewire (step 3); bridge active at drain (step 4)
  const queueConverted = step >= 3;
  const bridgeActive = step >= 4;
  const sourceDeleted = step >= 8;

  return (
    <div className="space-y-4">
      {/* Stepper */}
      <div className="panel p-4">
        <div className="mb-3 flex items-center justify-between">
          <div className="text-xs uppercase tracking-wider text-fg-muted">
            State machine — TLA+ verified, Lamport-ordered
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
                if (step >= CHOREOGRAPHY.length - 1) setStep(0);
                setPlaying((p) => !p);
              }}
              className="rounded bg-accent px-3 py-1 text-xs font-medium text-bg-base hover:opacity-90"
            >
              {playing ? "Pause" : step >= CHOREOGRAPHY.length - 1 ? "Replay" : "Play"}
            </button>
            <button
              onClick={() => setStep((s) => Math.min(s + 1, CHOREOGRAPHY.length - 1))}
              className="rounded border border-border-subtle px-3 py-1 text-xs hover:bg-bg-subtle"
            >
              Next →
            </button>
          </div>
        </div>

        {/* Step dots */}
        <div className="flex items-center gap-1">
          {CHOREOGRAPHY.map((s, i) => (
            <div key={i} className="flex flex-1 items-center">
              <button
                onClick={() => {
                  setStep(i);
                  setPlaying(false);
                }}
                className="flex flex-col items-center gap-1"
              >
                <div
                  className={
                    "h-3 w-3 rounded-full transition-all " +
                    (i < step
                      ? "bg-success"
                      : i === step
                        ? "bg-accent ring-2 ring-accent/40 scale-125"
                        : "bg-border")
                  }
                />
                <span
                  className={
                    "text-[10px] " +
                    (i === step ? "text-fg font-medium" : "text-fg-subtle")
                  }
                >
                  {s.label}
                </span>
              </button>
              {i < CHOREOGRAPHY.length - 1 && (
                <div
                  className={
                    "mx-1 h-px flex-1 transition-colors " +
                    (i < step ? "bg-success" : "bg-border-subtle")
                  }
                />
              )}
            </div>
          ))}
        </div>
      </div>

      {/* Visual: queue conversion diagram */}
      <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
        <div className="panel p-4 md:col-span-2">
          <div className="mb-2 text-xs uppercase tracking-wider text-fg-muted">
            Queue conversion — visual
          </div>
          <svg viewBox="0 0 700 220" className="w-full h-auto">
            <defs>
              <marker
                id="msg-arrow"
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
                Source QM (WQ22)
              </text>
              <text x={110} y={75} textAnchor="middle" className="fill-fg text-[10px] font-mono">
                RO.TH.WRQN.WQ22…
              </text>
              <text
                x={110}
                y={92}
                textAnchor="middle"
                className={
                  queueConverted
                    ? "fill-warn text-[9px] font-mono"
                    : "fill-success text-[9px] font-mono"
                }
              >
                {sourceDeleted
                  ? "TYPE(QREMOTE)"
                  : queueConverted
                    ? "TYPE(QREMOTE)"
                    : "TYPE(QLOCAL)"}
              </text>
              {queueConverted && (
                <text x={110} y={108} textAnchor="middle" className="fill-fg-subtle text-[8px] font-mono">
                  RQMNAME(APPQM_LDCWH_TH)
                </text>
              )}
              {/* XMITQ box (appears at rewire) */}
              {queueConverted && (
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
                    APPQM_LDCWH_TH.XMIT
                  </text>
                </g>
              )}
            </g>

            {/* Bridge channel arrow */}
            {bridgeActive && (
              <g>
                <line
                  x1={205}
                  y1={140}
                  x2={485}
                  y2={140}
                  stroke="#14b8a6"
                  strokeWidth={2}
                  markerEnd="url(#msg-arrow)"
                  className="animate-pulse"
                />
                <text x={345} y={132} textAnchor="middle" className="fill-accent text-[9px] font-mono">
                  WQ22.APPQM_.994F (SDR · RUNNING)
                </text>
                <text x={345} y={158} textAnchor="middle" className="fill-fg-subtle text-[8px]">
                  bridge channel
                </text>
              </g>
            )}
            {!bridgeActive && (
              <line
                x1={205}
                y1={140}
                x2={485}
                y2={140}
                stroke="#3f3f46"
                strokeWidth={1}
                strokeDasharray="4 4"
                opacity={0.5}
              />
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
                stroke={step >= 1 ? "#3b82f6" : "#3f3f46"}
                strokeWidth={1.5}
                opacity={step >= 1 ? 1 : 0.35}
              />
              <text x={580} y={40} textAnchor="middle" className="fill-fg-muted text-[10px] uppercase">
                Target QM (APPQM_LDCWH_TH)
              </text>
              {step >= 1 ? (
                <>
                  <text x={580} y={85} textAnchor="middle" className="fill-fg text-[10px] font-mono">
                    RO.TH.WRQN.WQ22…
                  </text>
                  <text x={580} y={102} textAnchor="middle" className="fill-success text-[9px] font-mono">
                    TYPE(QLOCAL)
                  </text>
                  <text x={580} y={118} textAnchor="middle" className="fill-fg-subtle text-[8px] font-mono">
                    CURDEPTH({bridgeActive ? "1" : "0"})
                  </text>
                </>
              ) : (
                <text x={580} y={120} textAnchor="middle" className="fill-fg-subtle text-[9px] italic">
                  (not yet provisioned)
                </text>
              )}
            </g>

            {/* Producer + Consumer */}
            <g>
              <circle cx={110} cy={195} r={6} fill="#a78bfa" />
              <text x={120} y={199} className="fill-fg-muted text-[9px] font-mono">
                RO (producer)
              </text>
              <circle cx={580} cy={195} r={6} fill="#3b82f6" />
              <text x={530} y={199} className="fill-fg-muted text-[9px] font-mono" textAnchor="end">
                LDCWH/TH (consumer)
              </text>
            </g>
          </svg>
        </div>

        <div className="panel p-4 space-y-3">
          <div>
            <div className="text-[10px] uppercase tracking-wider text-fg-subtle">
              Step {step + 1}/{CHOREOGRAPHY.length} · {CHOREOGRAPHY[step].label}
            </div>
            <div className="mt-1 font-mono text-sm text-accent">
              {CHOREOGRAPHY[step].state}
            </div>
          </div>
          <div>
            <div className="text-[10px] uppercase tracking-wider text-fg-subtle mb-1">
              MQSC
            </div>
            <div className="rounded bg-bg-subtle p-2 font-mono text-[10px] text-fg break-all">
              {CHOREOGRAPHY[step].mqsc}
            </div>
          </div>
          <div>
            <div className="text-[10px] uppercase tracking-wider text-fg-subtle mb-1">
              Why this step
            </div>
            <p className="text-xs text-fg-muted leading-relaxed">
              {CHOREOGRAPHY[step].rationale}
            </p>
          </div>
        </div>
      </div>
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────────
// View 3: Live data flow animation
// ─────────────────────────────────────────────────────────────────────────

function DataFlowView() {
  const [running, setRunning] = useState(false);
  const [pulse, setPulse] = useState(0);

  useEffect(() => {
    if (!running) return;
    const id = setInterval(() => setPulse((p) => (p + 1) % 100), 60);
    return () => clearInterval(id);
  }, [running]);

  // Positions for the three-node lane
  const producerX = 80;
  const sourceX = 250;
  const targetX = 540;
  const consumerX = 720;
  const laneY = 130;

  return (
    <div className="panel p-5">
      <div className="mb-3 flex items-center justify-between">
        <div>
          <div className="text-xs uppercase tracking-wider text-fg-muted">
            Bridge data flow — RO → LDCWH/TH (both migrated)
          </div>
          <div className="mt-0.5 text-[10px] text-fg-subtle font-mono">
            queue: RO.TH.WRQN.WQ22.HLN.YSRC.XL21
          </div>
        </div>
        <button
          onClick={() => setRunning((r) => !r)}
          className="rounded bg-accent px-3 py-1.5 text-xs font-medium text-bg-base hover:opacity-90"
        >
          {running ? "Stop" : "Start message flow"}
        </button>
      </div>

      <svg viewBox="0 0 800 260" className="w-full h-auto">
        <defs>
          <marker
            id="flow-arrow"
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
          <circle cx={producerX} cy={laneY} r={20} fill="#a78bfa" />
          <text x={producerX} y={laneY + 4} textAnchor="middle" className="fill-bg-base text-[10px] font-bold">
            P
          </text>
          <text x={producerX} y={laneY + 42} textAnchor="middle" className="fill-fg text-[10px] font-mono">
            RO
          </text>
          <text x={producerX} y={laneY + 56} textAnchor="middle" className="fill-fg-subtle text-[8px]">
            producer
          </text>
        </g>

        {/* Source QM (APPQM_RO) */}
        <g>
          <rect x={sourceX - 60} y={laneY - 40} width={120} height={80} rx={6} fill="#1a1a1a" stroke="#3f3f46" />
          <text x={sourceX} y={laneY - 24} textAnchor="middle" className="fill-fg-muted text-[9px] uppercase">
            QM
          </text>
          <text x={sourceX} y={laneY - 8} textAnchor="middle" className="fill-fg text-[10px] font-mono">
            APPQM_RO
          </text>
          <text x={sourceX} y={laneY + 8} textAnchor="middle" className="fill-success text-[8px] font-mono">
            QREMOTE
          </text>
          <text x={sourceX} y={laneY + 22} textAnchor="middle" className="fill-fg-subtle text-[7px] font-mono">
            → APPQM_LDCWH_TH.XMIT
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
          markerEnd="url(#flow-arrow)"
        />
        <text x={(sourceX + targetX) / 2} y={laneY - 12} textAnchor="middle" className="fill-accent text-[9px] font-mono">
          WQ22.APPQM_.994F (SDR · RUNNING)
        </text>
        <text x={(sourceX + targetX) / 2} y={laneY + 22} textAnchor="middle" className="fill-fg-subtle text-[8px]">
          bridge channel · TCP
        </text>

        {/* Target QM */}
        <g>
          <rect x={targetX - 60} y={laneY - 40} width={120} height={80} rx={6} fill="#1a1a1a" stroke="#3b82f6" strokeWidth={1.5} />
          <text x={targetX} y={laneY - 24} textAnchor="middle" className="fill-fg-muted text-[9px] uppercase">
            QM
          </text>
          <text x={targetX} y={laneY - 8} textAnchor="middle" className="fill-fg text-[10px] font-mono">
            APPQM_LDCWH_TH
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
          <circle cx={consumerX} cy={laneY} r={20} fill="#3b82f6" />
          <text x={consumerX} y={laneY + 4} textAnchor="middle" className="fill-bg-base text-[10px] font-bold">
            C
          </text>
          <text x={consumerX} y={laneY + 42} textAnchor="middle" className="fill-fg text-[10px] font-mono">
            LDCWH/TH
          </text>
          <text x={consumerX} y={laneY + 56} textAnchor="middle" className="fill-fg-subtle text-[8px]">
            consumer
          </text>
        </g>

        {/* Animated message dots */}
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

        {/* Annotations */}
        <text x={producerX} y={235} textAnchor="middle" className="fill-fg-subtle text-[8px] italic">
          amqsput to APPQM_RO
        </text>
        <text x={(producerX + sourceX) / 2} y={250} textAnchor="middle" className="fill-fg-subtle text-[8px] italic">
          (same connection string as pre-migration)
        </text>
        <text x={consumerX} y={235} textAnchor="middle" className="fill-fg-subtle text-[8px] italic">
          amqsget on APPQM_LDCWH_TH
        </text>
      </svg>

      <div className="mt-4 grid grid-cols-2 md:grid-cols-4 gap-3 text-xs">
        <Stat label="Producer reconnects" value="0" hint="zero — transparent rewiring" />
        <Stat label="Consumer reconnects" value="0" hint="zero — transparent rewiring" />
        <Stat label="Source-side hops" value="0" hint="data plane is QM→QM" />
        <Stat label="Avg roundtrip" value="~50ms" hint="LAN inside roco-dev namespace" />
      </div>
    </div>
  );
}

function Stat({ label, value, hint }: { label: string; value: string; hint: string }) {
  return (
    <div className="rounded border border-border-subtle bg-bg-subtle p-3">
      <div className="text-[9px] uppercase tracking-wider text-fg-subtle">
        {label}
      </div>
      <div className="mt-1 font-mono text-lg text-accent">{value}</div>
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
        <h1 className="text-2xl font-semibold tracking-tight">Topology visualization</h1>
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
