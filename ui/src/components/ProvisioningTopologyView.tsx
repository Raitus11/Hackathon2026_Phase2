"use client";

import { useMemo, useState } from "react";
import type { FlowSpec, QueueManager, Topology } from "@/lib/bcl-client";

/**
 * ProvisioningTopologyView — a read-only picture of what got provisioned.
 *
 * After Provision + Realize, this draws the queue managers as boxes and
 * the application flows as the channels connecting them, so a judge can
 * see at a glance how the peripheral MQ objects wire together. It writes
 * nothing and provisions nothing — it reads the topology that already
 * exists (Topology.queue_managers + Topology.spec.flows) and renders it.
 *
 * The drawing is a hand-built inline SVG. No graph library, no new npm
 * dependency — it cannot affect the build.
 *
 * Layout:
 *   - QM boxes are laid out in a column, ordered by how many flows touch
 *     them (busiest at the top) so the hubs are easy to spot.
 *   - Each Remote flow is an edge producer-QM → consumer-QM. Local flows
 *     (producer and consumer on the same QM) render as a self-loop badge.
 *   - Hovering a QM highlights only the flows that touch it.
 */

// ─────────────────────────────────────────────────────────────────────
// Data shaping
// ─────────────────────────────────────────────────────────────────────

interface QmObjects {
  channels: string[]; // distinct channel names touching this QM
  xmitqs: string[]; // distinct transmission queue names on this QM
  localQueues: string[]; // distinct app queue names hosted on this QM
  remoteQueues: string[]; // distinct queue names this QM routes out as QREMOTE
}

interface QmNode {
  name: string;
  qm: QueueManager | null; // null = referenced by a flow but not a deployed QM row
  flowCount: number;
  deployed: boolean;
  ready: boolean;
  objects: QmObjects;
}

interface FlowEdge {
  from: string; // producer QM
  to: string; // consumer QM
  flow: FlowSpec;
  isLocal: boolean;
}

function readFlows(topology: Topology): FlowSpec[] {
  // spec is typed as Record<string, unknown>; flows is an array of FlowSpec.
  const raw = (topology.spec as { flows?: unknown }).flows;
  if (!Array.isArray(raw)) return [];
  // Defensive: only keep entries that have the QM fields we draw with.
  return raw.filter(
    (f): f is FlowSpec =>
      !!f &&
      typeof f === "object" &&
      typeof (f as FlowSpec).producer_queue_manager === "string" &&
      typeof (f as FlowSpec).consumer_queue_manager === "string",
  );
}

function buildGraph(topology: Topology): {
  nodes: QmNode[];
  edges: FlowEdge[];
} {
  const flows = readFlows(topology);
  const qmByName = new Map<string, QueueManager>();
  for (const qm of topology.queue_managers) qmByName.set(qm.qm_name, qm);

  const counts = new Map<string, number>();
  const bump = (n: string) => counts.set(n, (counts.get(n) ?? 0) + 1);

  const edges: FlowEdge[] = flows.map((f) => {
    bump(f.producer_queue_manager);
    if (f.consumer_queue_manager !== f.producer_queue_manager) {
      bump(f.consumer_queue_manager);
    }
    // Trust the CSV's flow_type. A Remote flow can still have its
    // producer and consumer on the same shared QM — that does NOT make
    // it a local flow. Only flow_type === "Local" is a local flow.
    return {
      from: f.producer_queue_manager,
      to: f.consumer_queue_manager,
      flow: f,
      isLocal: f.flow_type === "Local",
    };
  });

  // Per-QM object inventory, derived from the flows. Each Remote flow
  // implies: a channel + XMITQ on the producer QM, a QREMOTE on the
  // producer QM, and a QLOCAL on the consumer QM. Local flows imply a
  // QLOCAL on the single QM. We collect distinct names per QM.
  const objByQm = new Map<string, QmObjects>();
  const objFor = (n: string): QmObjects => {
    let o = objByQm.get(n);
    if (!o) {
      o = { channels: [], xmitqs: [], localQueues: [], remoteQueues: [] };
      objByQm.set(n, o);
    }
    return o;
  };
  const pushUnique = (arr: string[], v: string | null | undefined) => {
    if (v && !arr.includes(v)) arr.push(v);
  };
  for (const e of edges) {
    const prod = objFor(e.from);
    const cons = objFor(e.to);
    if (e.isLocal) {
      // Local flow: producer and consumer share one QLOCAL on one QM.
      pushUnique(prod.localQueues, e.flow.producer_queue_name);
      pushUnique(prod.localQueues, e.flow.consumer_queue_name);
    } else {
      // Remote flow: producer side gets a QREMOTE + XMITQ + channel;
      // consumer side hosts the real QLOCAL.
      pushUnique(prod.channels, e.flow.channel_name);
      pushUnique(prod.xmitqs, e.flow.transmit_queue_name);
      pushUnique(prod.remoteQueues, e.flow.producer_queue_name);
      pushUnique(cons.channels, e.flow.channel_name);
      pushUnique(cons.localQueues, e.flow.consumer_queue_name);
    }
  }

  // Node set = every deployed QM ∪ every QM named by a flow.
  const names = new Set<string>();
  for (const qm of topology.queue_managers) names.add(qm.qm_name);
  for (const e of edges) {
    names.add(e.from);
    names.add(e.to);
  }

  const emptyObjects = (): QmObjects => ({
    channels: [],
    xmitqs: [],
    localQueues: [],
    remoteQueues: [],
  });

  const nodes: QmNode[] = [...names].map((name) => {
    const qm = qmByName.get(name) ?? null;
    return {
      name,
      qm,
      flowCount: counts.get(name) ?? 0,
      deployed: qm !== null,
      ready: qm?.is_ready ?? false,
      objects: objByQm.get(name) ?? emptyObjects(),
    };
  });

  // Busiest QMs first — hubs at the top read better.
  nodes.sort((a, b) => b.flowCount - a.flowCount || a.name.localeCompare(b.name));
  return { nodes, edges };
}

// ─────────────────────────────────────────────────────────────────────
// SVG geometry
// ─────────────────────────────────────────────────────────────────────

const BOX_W = 208;
const BOX_H = 66;
const ROW_GAP = 22;
const COL_X = 60; // left column (producers / hubs)
const COL_X2 = 430; // right column (consumers)
const TOP = 56;

interface Placed extends QmNode {
  x: number;
  y: number;
  col: 0 | 1;
}

/**
 * Two-column placement: a QM that only ever consumes goes in the right
 * column; everything else (produces, or both) goes left. This keeps the
 * common producer→consumer edge flowing left-to-right. QMs are still
 * ordered busiest-first within each column.
 */
function placeNodes(nodes: QmNode[], edges: FlowEdge[]): Placed[] {
  const produces = new Set(edges.map((e) => e.from));
  const placed: Placed[] = [];
  let leftRow = 0;
  let rightRow = 0;
  for (const n of nodes) {
    const consumerOnly = !produces.has(n.name) && n.flowCount > 0;
    const col: 0 | 1 = consumerOnly ? 1 : 0;
    const row = col === 0 ? leftRow++ : rightRow++;
    placed.push({
      ...n,
      col,
      x: col === 0 ? COL_X : COL_X2,
      y: TOP + row * (BOX_H + ROW_GAP),
    });
  }
  return placed;
}

// ─────────────────────────────────────────────────────────────────────
// Component
// ─────────────────────────────────────────────────────────────────────

export default function ProvisioningTopologyView({
  topology,
}: {
  topology: Topology;
}) {
  const { nodes, edges } = useMemo(() => buildGraph(topology), [topology]);
  const placed = useMemo(() => placeNodes(nodes, edges), [nodes, edges]);
  const [hover, setHover] = useState<string | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [detailed, setDetailed] = useState(false);

  const posByName = useMemo(() => {
    const m = new Map<string, Placed>();
    for (const p of placed) m.set(p.name, p);
    return m;
  }, [placed]);

  if (nodes.length === 0) {
    return (
      <div className="panel p-5">
        <h3 className="text-sm font-semibold tracking-tight">
          Provisioning Topology
        </h3>
        <p className="mt-2 text-xs text-fg-muted">
          No queue managers or flows found for this topology yet. Provision
          and realize the topology to see the connection graph.
        </p>
      </div>
    );
  }

  const leftCount = placed.filter((p) => p.col === 0).length;
  const rightCount = placed.filter((p) => p.col === 1).length;
  const rows = Math.max(leftCount, rightCount, 1);
  const svgH = TOP + rows * (BOX_H + ROW_GAP) + 20;
  const svgW = COL_X2 + BOX_W + 60;

  const remoteEdges = edges.filter((e) => !e.isLocal);
  const localCount = edges.length - remoteEdges.length;
  const deployedCount = nodes.filter((n) => n.deployed).length;
  const readyCount = nodes.filter((n) => n.ready).length;

  const edgeActive = (e: FlowEdge) =>
    hover === null || e.from === hover || e.to === hover;
  const nodeActive = (name: string) => {
    if (hover === null) return true;
    if (name === hover) return true;
    return edges.some(
      (e) =>
        (e.from === hover && e.to === name) ||
        (e.to === hover && e.from === name),
    );
  };

  return (
    <div className="panel p-5">
      <div className="mb-1 flex items-baseline justify-between gap-3">
        <h3 className="text-sm font-semibold tracking-tight">
          Provisioning Topology
        </h3>
        <div className="flex items-baseline gap-3">
          <button
            type="button"
            onClick={() => setDetailed((d) => !d)}
            className={`rounded border px-2 py-0.5 text-xs transition-colors ${
              detailed
                ? "border-info bg-info/15 text-info"
                : "border-border-subtle text-fg-muted hover:text-fg"
            }`}
          >
            {detailed ? "✓ Object detail" : "Show object detail"}
          </button>
          <span className="text-xs text-fg-muted">
            {topology.kind === "SOURCE" ? "source" : "target"} ·{" "}
            {nodes.length} QM{nodes.length === 1 ? "" : "s"} · {edges.length}{" "}
            flow{edges.length === 1 ? "" : "s"}
          </span>
        </div>
      </div>
      <p className="mb-3 text-xs text-fg-muted">
        What was provisioned and how it connects — queue managers and the
        application flows wiring them together. Read-only view of the
        deployed topology; nothing here provisions or changes anything.
      </p>

      {/* legend */}
      <div className="mb-2 flex flex-wrap gap-x-4 gap-y-1 text-xs text-fg-muted">
        <span className="flex items-center gap-1.5">
          <span className="inline-block h-2.5 w-2.5 rounded-sm border border-success bg-success/20" />
          ready ({readyCount})
        </span>
        <span className="flex items-center gap-1.5">
          <span className="inline-block h-2.5 w-2.5 rounded-sm border border-warn bg-warn/15" />
          deployed, not ready ({deployedCount - readyCount})
        </span>
        <span className="flex items-center gap-1.5">
          <span className="inline-block h-2.5 w-2.5 rounded-sm border border-fg-subtle bg-transparent" />
          referenced, not deployed ({nodes.length - deployedCount})
        </span>
        <span className="flex items-center gap-1.5">
          <svg width="22" height="8">
            <line
              x1="0"
              y1="4"
              x2="22"
              y2="4"
              stroke="#4493f8"
              strokeWidth="1.6"
            />
          </svg>
          remote flow ({remoteEdges.length})
        </span>
        {localCount > 0 && (
          <span>local flow ({localCount})</span>
        )}
      </div>

      <div className="overflow-x-auto rounded-md border border-border-subtle bg-bg-subtle p-2">
        <svg
          viewBox={`0 0 ${svgW} ${svgH}`}
          width="100%"
          style={{ minWidth: 560 }}
          fontFamily="'Segoe UI', system-ui, sans-serif"
          onMouseLeave={() => setHover(null)}
        >
          <defs>
            <marker
              id="ptArrow"
              markerWidth="9"
              markerHeight="9"
              refX="7"
              refY="3"
              orient="auto"
            >
              <path d="M0,0 L7,3 L0,6 Z" fill="#4493f8" />
            </marker>
            <marker
              id="ptArrowDim"
              markerWidth="9"
              markerHeight="9"
              refX="7"
              refY="3"
              orient="auto"
            >
              <path d="M0,0 L7,3 L0,6 Z" fill="#33414f" />
            </marker>
          </defs>

          {/* column captions */}
          <text x={COL_X} y="28" fill="#7d8a99" fontSize="10" fontWeight="700">
            PRODUCES / HUB
          </text>
          {rightCount > 0 && (
            <text
              x={COL_X2}
              y="28"
              fill="#7d8a99"
              fontSize="10"
              fontWeight="700"
            >
              CONSUMES ONLY
            </text>
          )}

          {/* edges first, so boxes sit on top */}
          {remoteEdges.map((e, i) => {
            const a = posByName.get(e.from);
            const b = posByName.get(e.to);
            if (!a || !b) return null;
            // exit right edge of producer, enter left edge of consumer
            const x1 = a.x + BOX_W;
            const y1 = a.y + BOX_H / 2;
            const x2 = b.x;
            const y2 = b.y + BOX_H / 2;
            const midX = (x1 + x2) / 2;
            const active = edgeActive(e);
            return (
              <path
                key={`e${i}`}
                d={`M${x1},${y1} C${midX},${y1} ${midX},${y2} ${x2},${y2}`}
                fill="none"
                stroke={active ? "#4493f8" : "#2b3742"}
                strokeWidth={active ? 1.5 : 1}
                markerEnd={
                  active ? "url(#ptArrow)" : "url(#ptArrowDim)"
                }
                opacity={active ? 0.9 : 0.4}
              />
            );
          })}

          {/* QM boxes */}
          {placed.map((p) => {
            const active = nodeActive(p.name);
            const isSel = selected === p.name;
            const stroke = !p.deployed
              ? "#3a4c5e"
              : p.ready
                ? "#2f9e69"
                : "#c98a2b";
            const fill = !p.deployed
              ? "#161f29"
              : p.ready
                ? "#16241c"
                : "#241f16";
            const selfLoop = edges.some(
              (e) => e.isLocal && e.from === p.name,
            );
            const o = p.objects;
            const objSummary = [
              `${o.channels.length} ch`,
              `${o.localQueues.length} QL`,
              o.remoteQueues.length ? `${o.remoteQueues.length} QR` : null,
              o.xmitqs.length ? `${o.xmitqs.length} XMITQ` : null,
            ]
              .filter(Boolean)
              .join(" · ");
            return (
              <g
                key={p.name}
                transform={`translate(${p.x}, ${p.y})`}
                opacity={active ? 1 : 0.35}
                onMouseEnter={() => setHover(p.name)}
                onClick={() =>
                  setSelected((s) => (s === p.name ? null : p.name))
                }
                style={{ cursor: "pointer" }}
              >
                <rect
                  width={BOX_W}
                  height={BOX_H}
                  rx="7"
                  fill={fill}
                  stroke={isSel ? "#4493f8" : stroke}
                  strokeWidth={isSel || hover === p.name ? 2 : 1.3}
                />
                <text
                  x="12"
                  y="20"
                  fill="#e6edf3"
                  fontSize="11.5"
                  fontWeight="600"
                >
                  {p.name.length > 24 ? p.name.slice(0, 23) + "…" : p.name}
                </text>
                <text x="12" y="35" fill="#8696a8" fontSize="8.5">
                  {p.deployed
                    ? p.ready
                      ? "deployed · ready"
                      : "deployed · starting"
                    : "referenced — not a deployed QM"}
                </text>
                <text x="12" y="48" fill="#5e6b78" fontSize="7.5">
                  {p.flowCount} flow{p.flowCount === 1 ? "" : "s"}
                  {p.qm ? ` · :${p.qm.listener_port}` : ""}
                </text>
                {/* object-count line — only in detailed mode */}
                {detailed && (
                  <text x="12" y="59" fill="#4d8fd6" fontSize="7.5">
                    {objSummary}
                    {p.qm ? ` · DLQ ${p.qm.dlq_name}` : ""}
                  </text>
                )}
                {selfLoop && (
                  <text
                    x={BOX_W - 10}
                    y="20"
                    fill="#7d8a99"
                    fontSize="8"
                    fontWeight="700"
                    textAnchor="end"
                  >
                    ↻ local
                  </text>
                )}
              </g>
            );
          })}
        </svg>
      </div>

      {/* click-to-expand object detail for a selected QM */}
      {selected &&
        (() => {
          const node = nodes.find((n) => n.name === selected);
          if (!node) return null;
          const o = node.objects;
          const Section = ({
            label,
            items,
          }: {
            label: string;
            items: string[];
          }) =>
            items.length === 0 ? null : (
              <div className="mb-2">
                <div className="mb-0.5 text-xs font-semibold text-fg-muted">
                  {label} ({items.length})
                </div>
                <div className="flex flex-wrap gap-1">
                  {items.map((it) => (
                    <span
                      key={it}
                      className="rounded bg-bg-subtle px-1.5 py-0.5 font-mono text-[10px] text-fg-subtle"
                    >
                      {it}
                    </span>
                  ))}
                </div>
              </div>
            );
          return (
            <div className="mt-3 rounded-md border border-info/40 bg-info/5 p-3">
              <div className="mb-2 flex items-center justify-between">
                <span className="font-mono text-sm font-semibold">
                  {node.name}
                </span>
                <button
                  type="button"
                  onClick={() => setSelected(null)}
                  className="text-xs text-fg-muted hover:text-fg"
                >
                  ✕ close
                </button>
              </div>
              <Section label="Channels" items={o.channels} />
              <Section label="Transmission queues" items={o.xmitqs} />
              <Section label="Remote queue definitions" items={o.remoteQueues} />
              <Section label="Local queues" items={o.localQueues} />
              {node.qm && (
                <div className="mt-1 text-xs text-fg-muted">
                  Dead-letter queue:{" "}
                  <span className="font-mono text-fg-subtle">
                    {node.qm.dlq_name}
                  </span>{" "}
                  · listener :{node.qm.listener_port}
                </div>
              )}
            </div>
          );
        })()}

      <p className="mt-2 text-xs text-fg-muted">
        Hover a queue manager to isolate the flows that touch it; click one to
        see its provisioned objects.{" "}
        {hover ? (
          <span className="text-fg-subtle">
            Showing flows for{" "}
            <span className="font-mono">{hover}</span>.
          </span>
        ) : (
          "Busiest queue managers are listed first."
        )}
      </p>
    </div>
  );
}
