# 📊 Output — Migration Evidence

**IBM MQ Hackathon 2026 · Phase 2 · Team intelliAI2DotO**

This folder holds the **concrete evidence** produced by IntelliAI 2.0 — the IBM MQ migration control plane. Where the [documentation](../docs/) explains *how* the system works, this folder *proves it ran*. For setup and execution, see the [repository README](../../README.md).

---

## Contents

| File / folder | What it holds |
|---|---|
| [`migration-plan.md`](./migration-plan.md) | The sequenced migration strategy — ordered steps, the 12-state choreography, dependencies, checkpoints, and the rollback strategy. |
| `migration-evidence/` | Per-step execution evidence — logs, screenshots, and reports showing each migration step executed through the BCL. |
| `validation-results/` | Validation reports and message-flow evidence — pre-, during-, and post-migration. |
| `rollback-evidence/` | Rollback trigger conditions and proof that rollback restored the last known good state. |
| `operability-evidence/` | Health-check, structured-logging, and end-to-end test evidence. |
| `supporting-artifacts/` | Optional — topology manifests, diagrams, and API payload examples. |

---

## 1. Migration plan and execution evidence

**Plan.** The sequenced migration strategy — ordered steps, dependencies, checkpoints, and safety criteria — is in [`migration-plan.md`](./migration-plan.md).

**Execution evidence** (`migration-evidence/`). For each application migrated, evidence that every step ran through the BCL:

- The MQSC script executed for the migration (exported per migration from the BCL).
- The Lamport-ordered audit trail for the migration (exported as CSV from the BCL).
- Screenshots or logs of the UI showing each state transition through the 12-state machine.
- **Transparent rewiring evidence** — proof that the application's producers and consumers continued to work with no connection change or restart across the cutover.

## 2. Validation results

`validation-results/` — evidence that message flow was correct before, during, and after each migration:

- **Pre-migration validation** — baseline message-flow health on the source path.
- **In-flight validation** — checks performed during the incremental migration.
- **Post-migration validation** — confirmation of correct end-state behaviour on the dedicated target.
- **Proof of message delivery** — the end-to-end test result (`amqsput` → poll consumer queue depth → `amqsget`) showing a real message traversed the migrated path, with no loss and no duplication.

## 3. Rollback evidence

`rollback-evidence/` — evidence that automated, per-application rollback works:

- The trigger condition that initiated the rollback (a failed step or a simulated failure).
- Evidence that the rollback restored the last known good state — the source queues re-established, the bridge objects removed.
- Logs, API responses, or UI screenshots showing the migration settled at `ROLLED_BACK`, with co-tenant applications untouched.

## 4. Test and operability evidence

`operability-evidence/` — evidence that the system is operable:

- **End-to-end test evidence** for the key operator flows.
- **Health-check evidence** — output from the liveness (`/health/live`) and readiness (`/health/ready`) endpoints, and the BCL health summary export.
- **Logging / observability evidence** — structured JSON log samples from the BCL.

## 5. Supporting artifacts (optional)

`supporting-artifacts/` — topology manifests, the architecture and state-machine diagrams, and API payload examples that help explain the migration flow.

---

## Evidence summary

The evidence in this folder maps directly to the hackathon judging criteria — provisioning, migration, validation, rollback, and operability:

| Folder | Evidence present |
|---|---|
| `migration-evidence/` | Per-migration MQSC script exports and the Lamport-ordered audit logs (CSV), plus UI screenshots of the 12-state machine progression. |
| `validation-results/` | Pre-, in-flight, and post-migration validation output, and the test-message-flow result (`amqsput` → poll depth → `amqsget`) proving message delivery with no loss and no duplication. |
| `rollback-evidence/` | The rollback trigger condition and restored-state proof — logs and screenshots showing a migration settled at `ROLLED_BACK` with co-tenant applications untouched. |
| `operability-evidence/` | Health-check output (liveness / readiness), structured log samples, and end-to-end test evidence. |
| `supporting-artifacts/` | Topology manifests, diagrams, and API payload examples (optional). |

The audit logs in `migration-evidence/` are the system of record: every state change is recorded with a Lamport clock, so the causal order of the migration is verifiable end to end.

---

## Notes

- The evidence here is intended to map directly to the judging criteria in [`problem.md`](../../problem.md).
- The required evidence is focused on **provisioning, migration, validation, rollback, and operability**.
- For large binaries, store them externally and add a link in the relevant subfolder.

---

*Team intelliAI2DotO — IBM MQ API Hackathon 2026, Phase 2.*
