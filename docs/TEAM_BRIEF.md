# IntelliAI 2.0 — Team Brief

**Team:** intelliAI2DotO &nbsp;·&nbsp; **Event:** Wells Fargo Hackathon 2026 — Phase 2

---

## What we're building

Phase 1's IntelliAI **analyzed** 13K rows of MQ topology and **recommended** a target shape.

IntelliAI 2.0 is the layer that **executes** that recommendation — a control plane that takes 7 apps currently sharing queue managers, migrates them one at a time onto their own dedicated queue managers, and proves nothing broke in the process.

Three properties that make it hackathon-grade rather than a script:

1. **Transparent rewiring.** Producers and consumers do not change their connection strings during migration. We move the queues underneath them using IBM MQ's XMITQ + Remote Queue + SDR/RCVR channel pattern.
2. **Per-app rollback.** If a migration step fails — or we deliberately induce a failure — we walk the audit log backward and undo it. The app goes back to its source-topology state.
3. **System of record.** Every state change (every MQSC command, every K8s object, every validation result) lands in a Lamport-clocked append-only audit log. The audit log *is* the evidence bundle.

---

## The problem in one paragraph

Wells Fargo runs many enterprise applications on shared IBM MQ queue managers. Sharing is operationally brittle: one app's bad day affects every other app on the same QM. The target architecture gives every app its own dedicated QM. But you cannot just rebuild everything from scratch — you have to migrate one app at a time, without breaking the others, with full audit trail and a working rollback path. That's the hackathon problem. Our job is the control plane that makes that migration safe, repeatable, and observable.

---

## Architecture in one diagram

```
┌─────────────────┐       REST       ┌────────────────────────────────────┐
│  UI Control     │ ───────────────▶ │  Business Control Layer (BCL)      │
│  Plane          │ ◀─────────────── │  FastAPI · Python 3.12              │
│  Next.js · 7    │   SSE streaming  │                                     │
│  tabs + chat    │                  │  ┌──────────┐  ┌─────────────────┐ │
└─────────────────┘                  │  │ Engines  │  │ Audit log       │ │
                                     │  │ provision│  │ Lamport clock   │ │
                                     │  │ rewire   │  │ SQLite + WAL    │ │
                                     │  │ validate │  │ on PVC          │ │
                                     │  │ rollback │  └─────────────────┘ │
                                     │  └──────────┘  ┌─────────────────┐ │
                                     │  ┌──────────┐  │ Agents          │ │
                                     │  │ Guardrails│ │ Migration planner│ │
                                     │  │ 10 rules │  │ Operator chat   │ │
                                     │  └──────────┘  └─────────────────┘ │
                                     └─────────────────┬──────────────────┘
                                                       │ kubectl exec + runmqsc
                                                       ▼
       ┌─────────────────────────────────────────────────────────────────┐
       │  OpenShift namespace · roco-dev                                  │
       │  ┌──────────────────┐  ┌──────────────────┐  ┌────────────────┐ │
       │  │ 3 Source QM pods │  │ 7 Target QM pods │  │ Producer +     │ │
       │  │ shared, 7 apps   │  │ one per app      │  │ Consumer pods  │ │
       │  │ (Day 1 baseline) │  │ (Day 9-13)       │  │ unchanged conn │ │
       │  └──────────────────┘  └──────────────────┘  └────────────────┘ │
       │                                                                  │
       │  All MQ pods: WF internal image  mq:9.4.5.0-r2                   │
       │  Storage:     NetApp Trident CSI (sc-ontap-nas)                  │
       │  Routes:      passthrough TLS to MQ web console on 9443          │
       └─────────────────────────────────────────────────────────────────┘
```

**Key invariant:** the BCL is the only thing that talks to MQ. The UI never touches MQ directly. Producer and consumer apps connect to MQ on 1414 inside the cluster using a stable DNS name that **never changes** during migration.

---

## Migration flow — what happens to one app

```
       PLANNED
          ▼
   PROVISIONING_TARGET_QM    ◀── BCL deploys APPQM_<app> pod
          ▼
   VALIDATING_PRE            ◀── confirm source still healthy
          ▼
   REWIRING                  ◀── new XMITQ + channel + Remote Queue
          ▼
   DRAIN_WAIT                ◀── poll source queue depth → 0
          ▼
   VALIDATING_DURING         ◀── messages flowing, none lost
          ▼
   DRAINING_SOURCE           ◀── teardown source queue
          ▼
   VALIDATING_POST           ◀── confirm new shape correct
          ▼
   COMPLETED
```

Any state can transition into `ROLLING_BACK` → `ROLLED_BACK`. The rollback engine walks the audit log in reverse-Lamport order and emits inverse MQSC for every forward step.

---

## OCP integration — how the BCL touches OpenShift

The BCL runs as a pod inside `roco-dev`. It needs a ServiceAccount with these permissions on the namespace (Helm chart in `infra/helm/intelliai2doto/`):

| Resource | Verbs | Why |
|---|---|---|
| `deployments` | create, get, delete, watch | Spin up / tear down QM pods |
| `services` | create, get, delete | ClusterIP for QM listeners |
| `secrets` | create, get | Per-QM MQ credentials |
| `routes.route.openshift.io` | create, get, delete | Web console passthrough |
| `persistentvolumeclaims` | create, get | BCL's own SQLite PVC |
| `pods` | get, list | Provisioner readiness checks |
| `pods/exec` | create | Run `runmqsc` inside QM pods |

**MQ command delivery** happens via `kubectl exec` into each QM pod, running `runmqsc` and capturing both the command and its `AMQxxxx` response into the audit log. The raw MQSC dialogue *is* the evidence — judges will recognize it instantly.

**State** lives in SQLite on a Trident PVC. We deliberately chose this over Postgres because it removes a separate pod, removes the database-provisioning conversation entirely, and the BCL is a single-writer system so SQLite's concurrency model is fine. WAL mode gives us crash-safety.

**LLM**: Tachyon (Gemini 2.5 Pro via langchain).

---

## How we'll demo

A 15-minute walkthrough following one app through the full lifecycle. The script for the live demo:

**0:00 — Topology.** Show the source topology in the UI. 7 apps, 3 shared source queue managers. Click into the graph. Producer/consumer pods are already running in `oc get pods` projected on a side screen.

**2:00 — Pick first migration target.** APUMN/GC. Click "Plan migration." The Migration Planner agent (LLM-backed) generates an ordered sequence of MQSC operations and surfaces it for operator approval.

**4:00 — Approve and execute.** Live stream of MQSC commands hitting the QM pod via `kubectl exec`. Each command and response shows in the audit log tab in real time. Lamport clock increments. UI topology graph animates: new APPQM_APUMN_GC pod comes up, channels wire themselves in.

**7:00 — Validate.** Four functional tests run automatically: connectivity, message-flow count consistency, functional put-get round-trip, app reconnect after target QM is in place. All four green.

**9:00 — The wow moment.** During the demo, **the producer and consumer apps' connection strings never changed.** Show the producer pod's environment variables on screen. Same DNS name, same port, all messages still flowing. The XMITQ pattern made it transparent.

**10:00 — Induce failure and rollback.** Click "Inject chaos: drop SDR channel." Validation fails. UI prompts "Roll back?" Approve. Rollback engine walks the audit log backward, emits reverse MQSC. App returns to source-topology state. All visible in the audit log, all reversible.

**12:00 — Big migration.** Kick off JUUD/C9 — 33 flows. Watch all 33 routing rules propagate through the audit log in seconds. End state: all 7 apps on their own QMs.

**14:00 — Evidence.** Click "Download evidence bundle." A zip per migration with: full audit log slice, MQSC commands, validation results, before/after topology snapshots. The kind of artifact that would actually survive a Wells Fargo internal audit.

---

## Why we'll medal

The judges will see three things teams typically don't get all three of:

1. **Real OCP.** Not a Docker Compose mock. Real `oc get pods -n roco-dev` showing `APPQM_LIY_KW`, `APPQM_JUUD_C9`. Real `oc exec` into a pod and `runmqsc` works.
2. **Real audit grade.** Lamport-clocked append-only log with full request/response payload, correlation IDs traceable end-to-end. Marc Oren can ask "who did this when and what was the response" — the answer is one `GET /audit/{lamport}` away.
3. **Real rollback.** Most teams will demo migration forward. We demo migration forward, then *break* it, then *roll it back*, then re-migrate. That sequence is what separates a script from a control plane.

Plus a UI that doesn't look generic — dark theme, framer-motion'd transitions, the design is the design system, not a Tailwind starter.

---

## Where the code is

- **Repo:** https://github.com/Raitus11/Hackathon2026_Phase2
- **Branch:** `main`
- **Local path:** `D:\Hackathon2026Phase2\intelliai2doto`
- **OCP namespace:** `roco-dev`

Run locally:
```
cd bcl && uvicorn bcl.api.main:app --reload --port 8080
```
Then open http://localhost:8080/docs for the live API.

For a deeper status report, see `docs/HANDOFF_2026-05-10.md`.

---

