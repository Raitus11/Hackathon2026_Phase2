# Migration Plan — IntelliAI 2.0

**IBM MQ Hackathon 2026 · Phase 2 · Team intelliAI2DotO**

This document describes the **sequenced migration strategy** IntelliAI 2.0 follows to move a production application off a shared IBM MQ queue manager onto a dedicated one. It is the plan; the evidence that the plan ran is in the sibling folders of [`artifacts/output/`](./) (see the [output README](./README.md)). For the system architecture, see [`../docs/architecture.md`](../docs/architecture.md).

---

## 1. Scope and unit of migration

The migration is performed **one application at a time**. Each application is migrated independently through its own state machine; a failure in one application's migration is contained to that application and does not affect any other.

- **Source estate** — applications consolidated onto a small number of *shared* queue managers.
- **Target estate** — one *dedicated* queue manager per application (a strict one-application-to-one-queue-manager rule).
- **Unit of migration** — a single application and the queues it owns on its shared source queue manager.

---

## 2. Pre-migration safety criteria

Before an application's migration is permitted to proceed, the BCL checks:

| Criterion | Requirement |
|---|---|
| Topology realized | The source topology is provisioned and its MQ objects are realized. |
| Target name resolved | The dedicated target queue manager name is derived and free of collision. |
| Blast-radius clear | The migration's full MQSC command set is enumerated; the count of commands touching a **co-tenant** application's queues is **zero**. The migration is gated on this. |
| No duplicate run | No non-terminal migration already exists for this application and target topology. |

If the blast-radius count is not zero, the migration does not run. Isolation is verified per plan, not assumed.

---

## 3. The sequenced migration — ordered steps

Each application moves through a fixed 12-state machine. The forward path is nine states; every transition emits MQSC and writes a Lamport-clocked audit record.

| # | State | Action | Checkpoint / safety criterion |
|---|---|---|---|
| 1 | `PLANNED` | The Migration Planner produces the per-application plan — bridge names, queues to rewire, identified risks. | Plan persisted on the migration record. |
| 2 | `PROVISIONING_TARGET_QM` | Deploy the dedicated target queue manager pod on OpenShift; realize its MQ objects (DLQ, channels, queues) via MQSC. | Provisioning is idempotent — safe to re-run. |
| 3 | `VALIDATING_PRE` | Confirm the target queue manager is reachable and the bridge channels are running. | Cutover does not begin until pre-validation passes. |
| 4 | `REWIRING` | For each of the application's source queues: delete the `QLOCAL`, define a `QREMOTE` of the **same name** pointing over the XMITQ bridge to the target. | The producer keeps the same connection and queue name — it is never touched. |
| 5 | `DRAIN_WAIT` | Predict the drain time of the old source queue using Little's Law. | Prediction surfaced; the engine still confirms by polling. |
| 6 | `VALIDATING_DURING` | Confirm messages flow over the new path while the old source queue drains. | In-flight validation. |
| 7 | `DRAINING_SOURCE` | Poll the old source queue depth until a confirmed zero — **three consecutive zero-depth polls** at 500 ms intervals, within a 300 s timeout. | The proof that no message was stranded at cutover. |
| 8 | `VALIDATING_POST` | Confirm the application is fully served by the dedicated target queue manager. | Post-migration validation. |
| 9 | `COMPLETED` | Remove the now-empty source queues. | The application owns a dedicated queue manager. |

### The bridge (step 4 detail)

The cutover routes over a transmission-queue bridge:

- A transmission queue (`QLOCAL` with `USAGE(XMITQ)`) and a sender channel (`CHLTYPE(SDR)`) on the source side.
- A matching receiver channel (`CHLTYPE(RCVR)`) on the target side. Sender and receiver share a channel name.
- Each application queue is then redefined `QLOCAL` → `QREMOTE` (same name) with `RNAME` / `RQMNAME` / `XMITQ` set so MQ's remote-queue routing forwards messages over the bridge.

Because the queue name and the producer's connection are unchanged, the cutover is transparent to the application.

---

## 4. Dependencies

- **OpenShift** — capacity for the dedicated target queue manager pod.
- **The source topology** — must be provisioned and realized before any migration.
- **Channel reachability** — the bridge channels between source and target must be running before `REWIRING`.
- **The existing consumers** — drive the drain in step 7; the drain completes as they consume the backlog.

---

## 5. Rollback strategy

Rollback is **automatic on failure** and **scoped to one application**.

- A failure at any forward state transitions the migration to `ROLLING_BACK`.
- The rollback engine walks that application's audit entries in **reverse Lamport order** and applies the inverse MQSC for each completed step — restoring the source queues, removing the bridge objects created for the target.
- Applications already `COMPLETED`, or still `PLANNED`, are never touched.
- The migration settles at `ROLLED_BACK` (source state restored, failure captured in the audit log) — or, if the rollback itself cannot complete, at the terminal `ROLLBACK_FAILED` state, which explicitly signals that human recovery is required.
- After `ROLLED_BACK`, the operator can restart: the record recycles to `PLANNED` and the idempotent forward path runs again.

---

## 6. Checkpoints summary

The plan has four explicit safety checkpoints, any of which can trigger rollback:

1. **Blast-radius gate** (pre-flight) — zero co-tenant commands, or the migration does not start.
2. **`VALIDATING_PRE`** — target reachable and channels up, or no cutover.
3. **`VALIDATING_DURING`** — messages flow over the new path, or roll back.
4. **`DRAINING_SOURCE`** — source queue confirmed empty (three zero-depth polls), or `DRAIN_FAILED` → roll back.
5. **`VALIDATING_POST`** — application fully served by the target, or roll back.

---

*Evidence that this plan executed — migration logs, validation reports, rollback proof, operability output — is collected in [`artifacts/output/`](./). Team intelliAI2DotO, IBM MQ API Hackathon 2026, Phase 2.*
