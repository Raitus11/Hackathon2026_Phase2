# 🚀 intelliAI2DotO — IBM MQ Control Plane Hackathon Submission

> **IntelliAI 2.0** — an IBM MQ migration control plane that moves production applications onto dedicated queue managers with no message loss, no application reconfiguration, and a complete audit trail.

**Exercised against a real fleet:** 16 IBM MQ 9.4.5 queue managers (9 shared source, 7 dedicated targets) on OpenShift — 7 applications migrated, each through an explicit 12-state machine, each rollback-capable, every change Lamport-clocked in an exportable audit log.

<!-- TODO before commit: replace <repo-url> in the clone command, the demo video link, and the four Team-table rows. -->

---

## 📖 Table of Contents

- [Problem Statement](#-problem-statement)
- [Solution Summary](#-solution-summary)
- [Key Capabilities](#-key-capabilities)
- [How the Migration Works](#-how-the-migration-works)
- [Hackathon Scope](#-hackathon-scope)
- [Tech Stack](#-tech-stack)
- [Repository Structure](#-repository-structure)
- [Getting Started](#-getting-started)
- [How to Run](#-how-to-run)
- [Demo](#-demo)
- [Submission Requirements](#-submission-requirements)
- [Team](#-team)

---

## 🧩 Problem Statement

This hackathon focuses on **IBM MQ topology migration and validation automation**.

Teams are expected to build:

- A **Business Control Layer (BCL)** that wraps IBM MQ administrative APIs and manages a fleet of queue managers as one logical control plane.
- A **UI control plane** that visualizes topology state, migration progress, validation outcomes, and rollback status.
- An automated workflow to **provision the source topology**, **migrate incrementally to the target topology**, **validate message flows**, and **roll back safely** if problems are detected.

The full challenge brief is available in [`problem.md`](./problem.md).

**The underlying problem.** Enterprise IBM MQ estates consolidate many applications onto a few shared queue managers. When several applications share one queue manager, they share a failure domain, a maintenance window, a security boundary, and a capacity ceiling — one application's incident becomes everyone's incident. The remedy is to give each application its own dedicated queue manager. Performing that migration on a live estate, without losing a message or asking an application to reconfigure, is the hard part this submission solves.

---

## 💡 Solution Summary

**IntelliAI 2.0** is the control plane that performs this migration while holding four constraints simultaneously: **no message loss**, **no application change**, **no collateral impact on co-tenant applications**, and **full auditability**.

The system is built around a **Business Control Layer (BCL)** — a single service that is the only component permitted to issue MQSC or talk to the OpenShift cluster. The UI talks only to the BCL; the BCL talks to MQ. Every operation therefore passes through one chokepoint where it is validated, sequenced, and recorded.

The migration is modelled as an explicit **12-state state machine**, run **per application**. Each application moves through a fixed forward sequence; every transition emits MQSC and writes an audit record carrying a **Lamport clock**, so the causal order of events is never ambiguous. If any step fails, the engine rolls **that application** back — and only that application.

**Design stance.** Deterministic, code-reviewed engines perform every state-changing action — provisioning, rewiring, draining, rollback. Three LLM-backed agents, integrated with the **Tachyon LLM gateway (Gemini 2.5 Pro)**, assist the operator with migration planning, operational questions, and failure diagnosis. The audit log — not the UI, not the agents — is the system of record.

This submission migrated **7 applications** across a fleet of **16 IBM MQ 9.4.5 queue managers** (9 source, 7 dedicated targets) on OpenShift namespace `roco-dev` — all 7 migrations completed.

For a detailed write-up, see [Solution Overview](./artifacts/docs/solution-overview.md) and [Architecture](./artifacts/docs/architecture.md). A complete self-contained explainer is provided at [`intelliai_explainer.html`](./artifacts/docs/intelliai_explainer.html).

---

## ⚡ Key Capabilities

- **Transparent rewiring** — an application's queue is swapped for a remote-queue alias of the same name; the producer keeps its connection and never reconnects. The cutover is invisible to the application.
- **12-state migration engine** — each application migrates through an explicit, named state machine; every transition emits MQSC and is audit-logged. No ad-hoc scripts.
- **Per-application rollback** — a failure at any step reverses that application's completed steps in reverse Lamport order, restoring its source queues. Scoped to one application; co-tenants are untouched.
- **Blast-radius isolation, proven** — before a migration runs, the BCL enumerates every MQSC command it will issue and confirms that zero commands touch a co-tenant's queues. The migration is gated on that count.
- **Drain detection** — the old source queue is polled to a confirmed zero (three consecutive zero-depth readings) before removal — the proof that no message was stranded at cutover.
- **Message-flow validation** — an end-to-end test (`amqsput` → poll depth → `amqsget`) proves a real message traverses the migrated path, before and after the change.
- **Lamport-clocked audit log** — every state change writes one causally-ordered record to the system of record; the full trail is exportable as evidence.
- **Three LLM-backed agents** — a Migration Planner, an Operator Assistant, and an RCA Assistant, integrated with the Tachyon LLM gateway (Gemini 2.5 Pro), assist the operator with planning, questions, and failure diagnosis.
- **Production-quality operability** — liveness/readiness endpoints, structured JSON logging, and per-migration evidence export.

---

## 🔧 How the Migration Works

For one application moving off a shared source queue manager `S` onto a new dedicated target `T`, the BCL:

1. **Provisions `T`** — deploys the dedicated queue manager pod on OpenShift and realizes its MQ objects (DLQ, channels, queues) via MQSC.
2. **Builds an XMITQ bridge** — a transmission queue and a sender channel on `S`, with the matching receiver channel on `T`. Sender and receiver share a name.
3. **Rewires per queue** — for each of the application's queues on `S`, deletes the `QLOCAL` and defines a `QREMOTE` of the *same name* in its place. MQ's remote-queue routing now forwards messages over the bridge to the real local queue on `T`. The producer keeps PUTting to the same queue name — it is never touched.
4. **Drains `S`** — polls the old source queue depth to a confirmed zero so no message is left behind.
5. **Validates and finalizes** — confirms the new path end-to-end, then removes the now-empty source queues.

If any step fails, the migration rolls back automatically — scoped to that one application. The full mechanism, including the per-application isolation guarantee, is in [Architecture](./artifacts/docs/architecture.md).

---

## 🎯 Hackathon Scope

Our submission demonstrates:

- **Source-topology provisioning through the BCL only** — queue manager pods deployed on OpenShift and their MQ objects realized via MQSC, with no direct MQ access from anywhere but the BCL.
- **Incremental migration from shared queue managers to application-dedicated queue managers** — one application at a time, each through its own state machine.
- **Transparent rewiring** so unaffected producers and consumers need no connection changes — the cutover swaps an application's local queue for a remote-queue alias over an XMITQ bridge; the application keeps the same connection string and queue name.
- **Validation before, during, and after migration** using real test producers and consumers — an `amqsput` places a message, the engine polls the consumer queue depth, an `amqsget` retrieves it.
- **Automated rollback** on failed migration or failed validation — scoped to the affected application, reversing its completed steps in reverse Lamport order.
- **Production-quality operability** with structured logging, health checks, OpenTelemetry instrumentation, and evidence export.

**Per-app isolation is proven, not assumed.** Before a migration runs, the BCL's blast-radius analysis enumerates every MQSC command the migration will issue and counts those touching a co-tenant's queues. That count is zero, and the migration is gated on it.

---

## 🛠️ Tech Stack

| Layer | Technology |
|---|---|
| UI Control Plane | Next.js / React (App Router), SWR |
| BCL API | Python 3.12 / FastAPI, Uvicorn, Pydantic v2 |
| State / Metadata | SQLite via aiosqlite (WAL mode), SQLAlchemy 2.0 async, Alembic |
| MQ Integration | IBM MQ 9.4.5; MQSC applied via `oc exec` into the QM pod; MQ admin REST for read paths |
| Platform | OpenShift; queue managers deployed as pods via the Kubernetes API |
| Intelligence | Three LLM-backed agents on the Tachyon LLM gateway (Gemini 2.5 Pro) |
| Observability | structlog (structured JSON logs), Prometheus client, OpenTelemetry |

---

## 📁 Repository Structure

```
├── README.md                  # This file
├── problem.md                 # The challenge brief
├── bcl/                        # Business Control Layer (FastAPI / Python)
│   └── src/bcl/
│       ├── api/                # HTTP routers — topology, provisioning, migration,
│       │                       #   message-flow, audit, assistant, RCA, reliability,
│       │                       #   blast-radius, exports, health
│       ├── provisioning/       # Provisioning engine, K8s client, MQSC derivation,
│       │                       #   MQ realize, naming, render
│       ├── migration/          # 12-state migration engine, choreography, drain
│       ├── rollback/           # Per-app rollback engine (reverse Lamport order)
│       ├── agents/             # Migration Planner, Operator Assistant, RCA Assistant
│       ├── analysis/           # Blast-radius analysis, Markov reliability model
│       ├── audit/              # Lamport clock, audit writer, audit middleware
│       ├── evidence/           # Evidence-bundle export
│       ├── llm/                # Tachyon LLM gateway client
│       ├── models/             # SQLAlchemy ORM + Pydantic API schemas
│       └── db/                 # Async SQLite session management
├── ui/                         # UI control plane (Next.js / React)
│   └── src/app/                # Dashboard, Migrations, Topology / Viz, RCA pages
└── artifacts/
    ├── demo/                   # Demo video, screenshots, pitch deck
    ├── docs/                   # Architecture, solution overview, explainer
    └── output/                 # Migration plans, validation & rollback evidence,
                                #   operability and test results
```

---

## 🏁 Getting Started

### Prerequisites

- **Git**
- **OpenShift access** with sufficient quota for the MQ fleet and supporting services
- **Edit access** to the required non-production project / namespace
- **Python 3.12** and **Node.js 20+**
- **`oc`** (OpenShift CLI), logged in to the target cluster — the BCL applies MQSC via `oc exec`
- Access to the **IBM MQ 9.4.5 queue manager container image** and any required credentials

### Installation

```bash
# Clone the repository
git clone <repo-url>
cd intelliai2doto-ih2

# Install BCL (backend) dependencies
cd bcl
pip install -e .

# Install UI dependencies
cd ../ui
npm install
```

---

## ▶️ How to Run

```bash
# 1. Authenticate to OpenShift FIRST — the BCL inherits this session
oc login <cluster-url> --token=<token>

# 2. Start the BCL API (from the bcl/ directory, in the same shell as `oc login`)
cd bcl
uvicorn bcl.api.main:app --host 0.0.0.0 --port 8000

# 3. Start the UI control plane (separate shell)
cd ui
npm run dev          # serves on http://localhost:3000
```

**Configuration.** The BCL reads settings from `BCL_`-prefixed environment variables (see `bcl/src/bcl/config.py`). Key settings: the OpenShift `namespace`, the IBM MQ container image reference, MQ credentials, and the LLM provider configuration (`tachyon`). MQ credentials and the Tachyon API key are supplied via environment variables or OpenShift secrets — never committed to the repository.

The UI calls the BCL over REST; it expects the BCL at `http://localhost:8000` by default. The BCL is configured to accept requests from the UI origin (`http://localhost:3000`). If either service runs on a different host or port, set the corresponding values accordingly.

**Operational note.** Start the BCL from the same terminal session used for `oc login`; the BCL uses that session's credentials to reach the cluster. SQLite is created automatically on first start.

### The provision → migrate → validate workflow

Once both services are running, drive the full workflow from the UI:

1. **Upload the source topology** and **Provision** — the BCL deploys the source queue manager pods and realizes their MQ objects.
2. **Open the Migration workspace**, upload the target topology, and **Migrate** an application — the engine runs the 12-state choreography: provision the dedicated target QM, build the XMITQ bridge and SDR/RCVR channel pair, rewire the application's queues to remote-queue aliases, drain the source queue to a confirmed zero, validate, and remove the source queues.
3. **Send a test message** on any flow to confirm the path end-to-end (`amqsput` → poll → `amqsget`).
4. **Roll back** an application at any point — the rollback engine reverses its completed steps in reverse Lamport order.

---

## 🎬 Demo

The demo shows the full operational story:

- Provisioning the **source topology** through the BCL.
- Viewing the topology in the **UI control plane**.
- Executing an **incremental migration** — one application through the complete 12-state choreography.
- Running **message-flow validation** before and after the change, proving transparent rewiring (the producer never reconnects).
- Demonstrating **per-app rollback** on a simulated failure — the affected application reverses cleanly while co-tenant applications are untouched.

- 🎬 **Demo video:** _TODO: paste link — or see [`artifacts/demo/`](./artifacts/demo/)_
- 🖼️ **Screenshots:** see [`artifacts/demo/`](./artifacts/demo/)

---

## 📦 Submission Requirements

All final deliverables are placed under the `artifacts/` folder:

| Folder | Contents |
|---|---|

| `architecture.md` | Architecture, solution overview, API and workflow documentation, 
| `solution-overview.md` |solution overview, API and workflow documentation, 
[`intelliai_explainer.html`](./artifacts/docs/intelliai_explainer.html) |


---

