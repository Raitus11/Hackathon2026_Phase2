# IntelliAI 2.0 — IBM MQ Migration Control Plane

**Team:** intelliAI2DotO
**Wells Fargo IBM MQ Hackathon 2026 — Phase 2**

A production-quality control plane for migrating IBM MQ applications from a shared-queue-manager source topology to a 1:1-app-to-QM target topology, one app at a time, with transparent rewiring.

Phase 1's IntelliAI proved we could analyze 13K rows and recommend a target topology. IntelliAI 2.0 is the execution layer — the BCL, the migration engine, the rewiring, the rollback, the evidence — turning the recommendation into reality on real OCP with real MQ.

## What this is

Three pieces:

1. **BCL (Business Control Layer)** — FastAPI service in `bcl/`. The only thing that talks to IBM MQ. Enforces all 10 enterprise guardrails. Lamport-clocked audit log. Per-app rollback engine. The system of record.
2. **UI Control Plane** — Next.js + Tailwind + shadcn dashboard in `ui/`. Talks only to the BCL. Topology, migration, validation, rollback, audit, health. Plus a global Operator Assistant chat panel.
3. **Test apps** — producer and consumer pods in `testapps/`. Their connection strings never change during migration. They prove transparent rewiring.

All runs on OpenShift namespace `roco-dev`, real IBM MQ 9.4.5 pods.

## Repo layout

```
bcl/                       FastAPI BCL service (Python 3.12)
├── src/bcl/
│   ├── api/               REST endpoints
│   ├── models/            Pydantic + SQLAlchemy models
│   ├── db/                DB session, Alembic
│   ├── engines/           Provisioning, Migration, Rewiring, Validation, Rollback
│   ├── mq/                kubectl-exec runmqsc client + admin REST client
│   ├── guardrails/        10 enterprise constraint validators
│   ├── audit/             Lamport-clocked audit log middleware
│   ├── topology/          Source/target topology models + diff
│   ├── migration/         Per-app state machine
│   ├── validation/        4 functional tests per FAQ Q15
│   ├── rollback/          Reverse-Lamport audit log walker
│   ├── evidence/          Per-app evidence bundle builder
│   ├── llm/               Provider-agnostic LLM client (Groq/Tachyon)
│   └── agents/            Migration Planner + Operator Assistant
├── tests/unit/
├── tests/integration/
└── alembic/versions/

ui/                        Next.js 14 control plane
├── src/app/               Routes per tab
├── src/components/        Shared components
└── src/styles/

testapps/
├── producer/
└── consumer/

infra/
├── helm/intelliai2doto/         Helm chart for the whole stack
└── manifests/             Raw k8s manifests for reference

observability/
├── grafana/dashboards/
└── prometheus/

docs/                      Architecture, API ref, runbook, migration plan, demo script
formal/                    State machine spec (informal), invariants
agents/                    Agent prompt files (versioned)
```

## Quickstart (development)

Prerequisites: Python 3.12.8, Node 20+, OpenShift CLI (`oc`), access to namespace `roco-dev`.

```bash
# 1. BCL setup (SQLite database is auto-created at ./local-data/bcl.db)
cd bcl
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
alembic upgrade head
uvicorn bcl.api.main:app --reload --port 8080

# 2. UI setup (separate terminal)
cd ui
npm install
npm run dev   # http://localhost:3000

# 3. Connect to OCP namespace for MQ pod work
oc login <wf-ocp-cluster>
oc project roco-dev
```

## Deploy to OpenShift

```bash
helm install intelliai2doto ./infra/helm/intelliai2doto \
  --namespace roco-dev \
  --values ./infra/helm/intelliai2doto/values-dev.yaml
```

## Running tests

```bash
cd bcl
pytest tests/unit              # fast, no infra needed
pytest tests/integration       # requires Postgres + OCP namespace access
```

## Key documents

- `docs/architecture.md` — system architecture
- `docs/migration-plan.md` — how migrations work
- `docs/rollback-mechanism.md` — how rollback works
- `docs/security.md` — security posture (CHLAUTH, SSL/TLS, MCAUSER)
- `docs/runbook.md` — operational runbook
- `docs/demo-script.md` — 15-minute demo walkthrough
- `formal/migration-state-machine.md` — state machine + invariants

## Team — intelliAI2DotO

- Raitus (lead): BCL architecture, migration engine, agents, integration
- Member 2: MQ image, MQSC, rewiring engine, validation
- Member 3: OCP, Helm, observability, CI, test apps
- Member 4: UI, topology viz, real-time streams, chat panel

## License

Internal Wells Fargo. Hackathon 2026.
