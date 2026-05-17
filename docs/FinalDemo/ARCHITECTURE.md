# IntelliAI 2.0 — Architecture

**IBM MQ Hackathon 2026 · Phase 2 · Team intelliAI2DotO**

This document describes the as-built architecture of IntelliAI 2.0: its tiers, components, the migration mechanism, the per-application isolation guarantee, the state machine, the mathematical models, and the data model. It is a companion to the [README](../../README.md) and the [Solution Overview](./SOLUTION_OVERVIEW.md). A self-contained visual explainer is also provided at [`intelliai_explainer.html`](./intelliai_explainer.html).

---

## 1. Overview

IntelliAI 2.0 is an IBM MQ migration control plane. It moves production applications off shared queue managers onto dedicated ones, on a live estate, while holding four constraints at once:

- **No message loss** — every in-flight message is delivered; none stranded, none duplicated.
- **No application change** — producers and consumers keep the same connection string and queue name; they never reconnect.
- **No collateral impact** — migrating one application does not disturb co-tenant applications on the same shared queue manager.
- **Full auditability** — every change is recorded, causally ordered, and reversible.

The system was exercised end-to-end against a real fleet of **16 IBM MQ 9.4.5 queue managers** (9 source, 7 dedicated targets) on OpenShift namespace `roco-dev`, migrating **7 applications**.

---

## 2. System architecture

Three tiers, with a strict dependency direction: the UI talks only to the BCL; the BCL is the only component that talks to MQ.

![System architecture](./diagram-1-architecture.svg)

### 2.1 UI control plane

A Next.js / React application. It is a pure client of the BCL's REST API — it holds no MQ credentials and issues no MQSC. It presents four areas: a **Dashboard** (estate at a glance), **Migrations** (per-application state, step history, the Lamport audit timeline, drain prediction, the Operator Assistant, and the reliability panel), **Topology Viz** (source and target topology with the blast-radius view), and **RCA** (the RCA Assistant's diagnosis, a free-text question box, and evidence downloads).

### 2.2 Business Control Layer (BCL)

A FastAPI / Python service. It is the single chokepoint through which every queue manager operation passes — which is what makes the operation validatable, sequenceable, and auditable. The BCL contains four kinds of component:

- **Deterministic engines** — provisioning, the migration state machine, rewiring choreography, drain detection, rollback, MQSC derivation, and policy guardrails. Every state-changing action is performed here, by code-reviewed deterministic logic.
- **Agents** — three LLM-backed agents (Migration Planner, Operator Assistant, RCA Assistant) integrated with the Tachyon LLM gateway running Gemini 2.5 Pro. The agents are advisory: they plan, explain, and diagnose. They do not perform state changes.
- **Analysis modules** — blast-radius analysis and the Markov reliability model.
- **The audit log** — an SQLite-backed, Lamport-clocked record of every state change. It is the system of record.

The BCL applies MQSC by executing `runmqsc` inside the target queue manager pod via `oc exec`; it uses the MQ administrative REST interface for read paths.

### 2.3 OpenShift / IBM MQ tier

Real IBM MQ 9.4.5 queue managers, deployed as pods on OpenShift namespace `roco-dev`. The BCL provisions these pods through the Kubernetes API and realizes their MQ objects (dead-letter queue, local queues, channels) by piping MQSC into the pod. Source queue managers are shared by multiple applications; each target queue manager is dedicated to exactly one application.

---

## 3. The BCL as a single chokepoint

A design rule the system enforces structurally: **nothing issues MQSC or touches the OpenShift cluster except the BCL.** The UI cannot reach MQ; it can only call the BCL's REST API. This is what allows three properties to hold for *every* operation without exception:

1. **Validation** — a guardrail check runs before any state-changing call is admitted.
2. **Sequencing** — operations are ordered by the migration engine; concurrent edits cannot interleave unsafely.
3. **Recording** — audit middleware writes a Lamport-clocked record for every state change before the response returns.

Because there is one path, there is one place to enforce these — and one place a reviewer needs to inspect to be sure they hold.

---

## 4. The migration mechanism

The migration is performed per application. For an application currently hosted on a shared source queue manager `S`, moving to a dedicated target queue manager `T`:

**Step 1 — Provision the target.** The BCL deploys the pod for `T` via the Kubernetes API and realizes its MQ objects (DLQ, channels, queues) via MQSC. This step is idempotent.

**Step 2 — Build the XMITQ bridge.** On the source side the BCL defines a transmission queue (`QLOCAL` with `USAGE(XMITQ)`) and a sender channel (`CHANNEL` of `CHLTYPE(SDR)`) whose `XMITQ` is that transmission queue and whose `CONNAME` points at the target queue manager's cluster service on port 1414. On the target side it defines the matching receiver channel (`CHLTYPE(RCVR)`). The sender and receiver share a channel name. The channel-name and transmission-queue-name derivations are pure functions in the rewiring choreography module.

**Step 3 — Per-queue rewire (the cutover).** For each of the application's queues on `S`, the BCL deletes the `QLOCAL` and defines a `QREMOTE` in its place — same queue name — with `RNAME`, `RQMNAME`, and `XMITQ` set so that MQ's remote-queue routing forwards messages over the bridge to the real local queue on `T`. **The producer is unaffected:** it continues to `PUT` to the same queue name on the same connection; MQ now routes the message to `T`. This is what makes the cutover invisible to the application.

**Step 4 — Drain the source.** Messages already on the old source queue at cutover time are consumed by the existing consumers. The BCL polls the source queue depth until it reaches a confirmed zero (see §6.1) — the proof that no message was stranded.

**Step 5 — Validate and finalize.** After a post-cutover validation check, the now-empty source queues are removed; the remote-queue alias remains.

![Migration request flow](./diagram-3-migration-flow.svg)

---

## 5. Per-application isolation — proven, not assumed

A shared source queue manager hosts several applications. Migrating one must not disturb the others. IntelliAI 2.0 does not merely intend this — it checks it.

The rewiring is **per-queue**: only the migrating application's own queues are deleted and redefined. Co-tenant queues are never named in the migration's MQSC.

Before a migration runs, the **blast-radius analysis** module enumerates the complete set of MQSC commands the migration will issue and counts those that touch a co-tenant's queues. For a correct migration that count is **zero**, and the migration is gated on it. The analysis distinguishes the application's own queues, the queue managers it is hosted on, its co-tenant applications, and the queues those co-tenants own — and reports the isolation check as a structured result surfaced in the Topology Viz page.

Isolation is therefore a verified property of each specific migration plan, not a general assurance.

---

## 6. The migration state machine

Each application's migration is an explicit **12-state machine**, not a script. Modelling it this way means every application is always in a known, named state; every transition is audit-logged; and recovery is well-defined.

![State machine](./diagram-2-state-machine.svg)

**Forward path (nine states):**

```
PLANNED → PROVISIONING_TARGET_QM → VALIDATING_PRE → REWIRING
→ DRAIN_WAIT → VALIDATING_DURING → DRAINING_SOURCE
→ VALIDATING_POST → COMPLETED
```

**Failure path (three states):** from any non-terminal forward state the engine can transition to `ROLLING_BACK`, then settle at `ROLLED_BACK` (the application's source state restored, the failure captured in the audit log) or, if the rollback itself cannot complete, at `ROLLBACK_FAILED` (terminal — requires human recovery).

**Terminal states:** `COMPLETED`, `ROLLED_BACK`, `ROLLBACK_FAILED`.

**Restart.** After `ROLLED_BACK`, the operator can restart the migration: the record recycles to `PLANNED` and the forward path — which is idempotent — runs again from the start.

The transition rules live in a pure, I/O-free module so the state machine can be unit-tested and reasoned about independently of the engine. Every mutation of a migration's state passes through a single `assert_transition` chokepoint, so an out-of-order transition cannot occur. This module is the executable mirror of a next-state relation in the style of Lamport's *Specifying Systems* (2002) — the project uses that discipline; it does not run a model checker.

### 6.1 Drain detection

At cutover the engine must confirm the old source queue is empty before removing it. A single zero-depth reading is insufficient — between two polls a slow consumer could momentarily empty the queue while a message is still in transit. The engine therefore requires **three consecutive zero-depth polls** (with zero `IPPROCS`/`OPPROCS`), spaced at sub-second intervals, before declaring the drain complete. The probe also distinguishes "queue truly empty" from "queue removed" from other conditions.

---

## 7. Rollback

If any step fails, the **rollback engine** reverses that application's completed steps. It reads the application's audit entries and walks them in **reverse Lamport order**, applying the inverse MQSC for each step — restoring the source queues, removing the bridge objects created for the target, and so on.

Rollback is **scoped to one application**. Rolling back application A walks only A's audit entries and touches only A's queues; applications B and C — whether `COMPLETED` or still `PLANNED` — are never modified. This is the same per-queue, per-application discipline that gives the forward path its isolation guarantee.

---

## 8. The mathematics

Two well-established models are used — each where it genuinely applies, and each cited.

### 8.1 Little's Law — drain-time prediction

At cutover the engine waits for the old source queue to empty. Rather than poll blindly, it predicts how long that will take. With the producer rewired away, no new messages arrive at the old queue, so the standard queueing relationship reduces to a direct estimate:

```
L = λW   →   T_drain ≈ L₀ / μ
```

where `L₀` is the observed queue depth at rewire time and `μ` is the consumer service rate, measured from the rate at which the depth decreases.

The prediction is surfaced in the UI's drain widget; the engine still confirms the actual drain by polling depth to a confirmed zero (§6.1). The prediction informs the operator — the polling proves the result.

> Reference: Little, J. D. C. (1961). "A Proof for the Queuing Formula: L = λW." *Operations Research*, 9(3), 383–387.

### 8.2 Absorbing Markov chain — reliability model

The migration state machine is analysed as an **absorbing Markov chain**: `COMPLETED`, `ROLLED_BACK`, and `ROLLBACK_FAILED` are absorbing states; the rest are transient. From the transition structure the system computes the standard fundamental-matrix quantities — the expected number of steps to absorption, and the probability of ending in each absorbing state:

```
N = (I − Q)⁻¹
```

where `Q` is the transient-to-transient transition submatrix and `N` is the fundamental matrix.

The UI presents this reference model **alongside** a separate empirical transition estimate counted from the real audit log. The two are shown side by side and never conflated: one is a model, the other is a measurement.

> Reference: Kemeny, J. G. & Snell, J. L. (1960). *Finite Markov Chains*. Van Nostrand. (Fundamental matrix, Chapter 3.)

---

## 9. The agents

Three agents, all integrated with the Tachyon LLM gateway (Gemini 2.5 Pro). They are **advisory** — they inform the operator and never perform a state change. Every agent invocation is recorded in the `agent_invocations` table, and each agent has a per-invocation tool-call budget and a per-minute rate limit.

- **Migration Planner** — for a given application, produces a structured migration plan: a short narrative an operator reads before approving, the ordering rationale, identified risks, and the bridge channel / transmission-queue names involved. The plan is persisted on the migration record. (The same module also generates a completion narrative when a migration finishes.)
- **Operator Assistant** — answers free-text operational questions. It classifies the question's intent, assembles read-only context from migrations, the audit log, and recent agent invocations, and responds.
- **RCA Assistant** — for a failed migration, reads the Lamport-ordered audit trail, locates the failure event, identifies the MQ reason code, and produces a structured diagnosis with suggested checks. It also answers free-text questions about a specific migration.

The division of responsibility is deliberate: **deterministic engines decide and act; agents explain and advise; the audit log records.**

---

## 10. Data model

State is held in SQLite (WAL mode, accessed asynchronously via SQLAlchemy). The principal tables:

| Table | Purpose |
|---|---|
| `topologies` | Source and target topology definitions |
| `queue_managers` | Per-QM detail within a topology |
| `applications` | Applications and their queue ownership |
| `migrations` | One row per application migration, carrying current state and the persisted plan |
| `migration_steps` | Per-step history within a migration |
| `validation_runs` | Functional validation results |
| `audit_log` | Lamport-clocked record of every state change — the system of record |
| `agent_invocations` | Every agent call, with inputs, outputs, and timing |
| `evidence_bundles` | Exported evidence bundle metadata |
| `provision_runs`, `mq_realize_runs` | Provisioning and MQ-object realization run history |
| `knowledge_entries` | Knowledge-base entries supporting RCA |

Every state-changing audit entry uses a typed `AuditOperation` (for example `MQSC_DEFINE_QREMOTE`, `MQSC_DEFINE_QXMIT`, `MQSC_DEFINE_CHANNEL_SDR`, `MIGRATION_STATE_TRANSITION`), so the audit log is queryable by operation kind, not just free text.

---

## 11. REST API surface

The BCL exposes a documented REST API; OpenAPI is served by FastAPI at `/docs`. Principal routes:

| Area | Routes |
|---|---|
| Topology | `POST /topologies`, `GET /topologies`, `GET /topologies/{id}`, `GET /topologies/{id}/applications` |
| Provisioning | `POST /topologies/{id}/provision`, `POST /topologies/{id}/realize-mq-objects`, plus status routes |
| Message flow | `POST /topologies/{id}/test-message-flow` |
| Migration | `POST /migrations`, `GET /migrations/{id}`, `GET /migrations/{id}/plan`, `GET /migrations/{id}/drain`, `GET /migrations/{id}/audit`, `POST /migrations/{id}/rollback` |
| Analysis | `GET /topologies/{id}/blast-radius`, `GET /topologies/{id}/migration-order`, `GET /reliability/markov` |
| Agents | `POST /assistant/query`, `GET /rca/migrations/{id}`, `POST /rca/ask` |
| Audit | `GET /audit`, `GET /audit/{lamport}` |
| Exports | `GET /exports/migrations/{id}/mqsc-script`, `GET /exports/audit.csv`, `GET /exports/healthz/summary`, `GET /exports/migrations/{id}/evidence` |
| Health | `GET /health/live`, `GET /health/ready` |

---

## 12. Operability

- **Health** — separate liveness (`/health/live`) and readiness (`/health/ready`) endpoints.
- **Structured logging** — JSON logs via structlog, carrying correlation identifiers.
- **Metrics and tracing** — Prometheus client metrics and OpenTelemetry instrumentation on the FastAPI application.
- **Evidence export** — per-migration evidence bundles, MQSC script export, and an audit CSV export, so a completed migration can be reviewed offline.

---

## 13. Security posture

- **Single chokepoint** — only the BCL holds cluster and MQ credentials; the UI has none. This bounds the attack surface to one service.
- **Audit log as system of record** — every state change is recorded with a Lamport clock before the operation's response returns, giving an unambiguous, causally ordered history.
- **Channel configuration** — the BCL manages channel and channel-authentication objects as part of MQSC realization. Production deployments apply explicit per-peer channel-authentication rules so that only authorized partner queue managers can connect over the migration bridges.
- **Secrets** — MQ credentials and the Tachyon API key are supplied via environment variables or OpenShift secrets and are not committed to the repository.

---

## 14. Honest scope

Stated plainly, so the architecture is not over-read:

- The state machine discipline mirrors a formal next-state relation; the project **does not run a model checker** and does not claim machine-checked correctness.
- The Markov reliability figure is a **model** computed from the transition structure; it is presented next to — never merged with — the empirical estimate from the audit log.
- The Little's Law value is a **prediction**; the actual drain is always confirmed by polling.
- Cross-application message dependencies (for example shared reply-to relationships spanning applications) are out of scope for this submission.

The system is precise about what it proves and what it predicts. That distinction is part of the design.

---

*Team intelliAI2DotO — IBM MQ API Hackathon 2026, Phase 2.*
