# IntelliAI 2.0 — Solution Overview

**IBM MQ Hackathon 2026 · Phase 2 · Team intelliAI2DotO**

This document explains what IntelliAI 2.0 does, the problem it solves, *why* it is built the way it is, and how it maps to the hackathon scope. It sits between the [README](../../README.md) — the headline — and the [Architecture](./ARCHITECTURE.md) document — the full mechanism. For an interactive visual walkthrough, see [`intelliai_explainer.html`](./intelliai_explainer.html).

---

## 1. The problem

Enterprise IBM MQ estates consolidate many applications onto a few shared queue managers. That is operationally fragile.

When several applications share one queue manager, they share a **failure domain**, a **maintenance window**, a **security boundary**, and a **capacity ceiling**. One application's incident becomes everyone's incident. A patch for one application's queue manager forces an outage window on all the others. A traffic spike from one application starves the rest. The remedy is well understood: give each application its own dedicated queue manager.

The hard part is performing that migration on a **live estate**. A safe migration has to satisfy four constraints at the same time:

1. **No message loss** — every in-flight message must be delivered. None stranded on the old queue, none duplicated.
2. **No application change** — producers and consumers cannot be asked to change connection strings or restart. They must not even notice.
3. **No collateral impact** — migrating one application off a shared queue manager must not disturb the other applications still on it.
4. **Full auditability** — every change must be recorded — what ran, when, in what order, with what result — so the migration can be reviewed and, if needed, reversed.

IntelliAI 2.0 is the control plane that performs the migration while holding all four.

---

## 2. The solution in one paragraph

IntelliAI 2.0 is a **Business Control Layer (BCL)** — one service that is the sole point of contact with the IBM MQ estate and the OpenShift cluster — fronted by a **UI control plane**. The migration of each application is run as an explicit **12-state machine**: every transition emits MQSC and writes an audit record stamped with a Lamport clock, so the causal order of events is never ambiguous. Deterministic, code-reviewed engines perform every state-changing action. Three LLM-backed agents, integrated with the Tachyon LLM gateway (Gemini 2.5 Pro), assist the operator with planning, operational questions, and failure diagnosis. The audit log is the system of record. If any step fails, the engine rolls **that application** back — and only that application.

---

## 3. How a migration works

For one application, moving from a shared source queue manager to a new dedicated target queue manager, the system:

1. **Provisions the target** — deploys the dedicated queue manager pod on OpenShift and realizes its MQ objects (dead-letter queue, channels, queues).
2. **Builds a bridge** — a transmission-queue / sender-channel / receiver-channel path from the source queue manager to the new target.
3. **Rewires transparently** — replaces the application's local queue with a remote-queue alias of the *same name* pointing over the bridge. The producer keeps its connection and its queue name; MQ now routes its messages to the target. The application never reconnects and never notices.
4. **Drains the source** — waits for messages already on the old queue to be consumed, polling the depth to a confirmed zero so nothing is left behind.
5. **Validates and finalizes** — confirms the new path works end-to-end, then removes the now-empty source queues.

If anything fails at any step, the migration enters rollback: the engine reverses that application's completed steps in reverse Lamport order and restores its source queues, with the failure captured in the audit log.

---

## 4. A worked example

To make the mechanism concrete, here is one application's migration end to end. The target estate places seven applications onto seven dedicated queue managers under a strict one-application-to-one-queue-manager rule; the application `LIY/KW` is one of them.

**Starting point.** `LIY/KW` is a producer application hosted on a *shared* source queue manager — one of nine shared queue managers in the source estate, co-located with other applications. Its target is a new dedicated queue manager, `APPQM_LIY_KW`.

**The migration, state by state:**

| State | What happens for `LIY/KW` |
|---|---|
| `PLANNED` | The Migration Planner produces the plan: the bridge channel and transmission-queue names, the source queues to rewire, the identified risks, the rollback approach. |
| `PROVISIONING_TARGET_QM` | `APPQM_LIY_KW` is deployed as an OpenShift pod and its MQ objects are realized via MQSC. |
| `VALIDATING_PRE` | The BCL confirms `APPQM_LIY_KW` is reachable and the bridge channels are running before any cutover. |
| `REWIRING` | Each of `LIY/KW`'s queues on the shared source is deleted as a `QLOCAL` and redefined as a `QREMOTE` of the same name, pointing over the bridge. From this moment the producer's messages route to `APPQM_LIY_KW` — with no change on the producer side. |
| `DRAIN_WAIT` | The engine predicts how long the old source queue will take to empty, using Little's Law (see §6). |
| `VALIDATING_DURING` | A message-flow check confirms traffic reaches the target over the new path while the old queue drains. |
| `DRAINING_SOURCE` | The old source queue depth is polled to a confirmed zero — three consecutive zero-depth readings — proving no message was stranded at cutover. |
| `VALIDATING_POST` | A final check confirms `LIY/KW` is fully served by `APPQM_LIY_KW`. |
| `COMPLETED` | The now-empty source queues are removed. `LIY/KW` owns a dedicated queue manager. |

Crucially, while `LIY/KW` migrates, **every other application on that shared source queue manager is untouched** — the rewiring names only `LIY/KW`'s own queues, and the blast-radius check (§5) confirms that before the migration is allowed to run. The same choreography then runs, one at a time, for each of the remaining applications.

---

## 5. What makes it safe

| Constraint | How IntelliAI 2.0 holds it |
|---|---|
| No message loss | Drain detection polls the source queue to a confirmed zero — three consecutive zero-depth readings — before the source queue is removed. |
| No application change | Transparent rewiring: a remote-queue alias keeps the queue name and connection identical. The producer is never touched. |
| No collateral impact | Blast-radius analysis enumerates every MQSC command a migration will issue and confirms that **zero** commands touch a co-tenant's queues. The migration is gated on that count. |
| Full auditability | Every state change writes a Lamport-clocked audit record. The audit log — not the UI, not the agents — is the system of record, and it can be exported as evidence. |

The recurring theme: each guarantee is **checked**, not asserted. Isolation is verified per migration plan. The drain is proven by polling. The audit trail is written before each operation returns.

---

## 6. Why these design decisions

An overview should explain not just *what* was built but *why* — including the alternatives that were considered and set aside.

**Why a remote-queue alias, not an MQ cluster.** Joining source and target into a cluster would also route messages, but it changes the estate's topology globally and is far harder to reverse cleanly. A per-queue `QLOCAL`→`QREMOTE` swap is local, reversible by a single inverse command, and invisible to the application. Reversibility is the deciding factor: every forward step must have a clean inverse.

**Why per-application state machines, not one batch migration.** Migrating all applications in one operation would be faster on the happy path and unrecoverable on the unhappy one — a failure midway leaves the estate in an ambiguous mixed state. Running each application through its own state machine means a failure is contained to one application, the rollback is scoped to one application, and the other applications' migrations are unaffected. Isolation of *failure* matters more than speed.

**Why an explicit state machine, not a script.** A script that fails leaves the system in whatever state the script reached — which the operator must then reverse-engineer. An explicit state machine means every application is always in a *named* state, every transition is audit-logged, and recovery is defined for every state. The transition rules live in a pure, I/O-free module with a single `assert_transition` chokepoint, so an out-of-order transition cannot occur.

**Why deterministic engines for every state change, with agents only advising.** An LLM is excellent at planning, explanation, and diagnosis and unsuitable for issuing the MQSC that mutates a production queue manager. The split is deliberate: deterministic, code-reviewed engines decide and act; the agents plan, explain, and diagnose; the audit log records. The parts of the system that change MQ state are predictable and reviewable.

**Why SQLite as the state store.** The BCL's state — topologies, migrations, the audit log — is modest in volume and is written by a single service. SQLite in WAL mode gives transactional integrity and crash safety without operating a separate database. It is the right tool at this scale; a larger estate is a natural place to revisit it.

**Why only two mathematical models.** The system uses Little's Law and an absorbing Markov chain — and no more. Little's Law earns its place because the drain is the one point where a *prediction* genuinely helps the operator rather than blind polling. The absorbing Markov chain earns its place because the migration state machine *is*, structurally, an absorbing chain — so the reliability quantities fall out of a model that already exists. Neither is decoration; each maps to a real question the system needs to answer.

---

## 7. The role of intelligence

Three agents, all on the Tachyon LLM gateway (Gemini 2.5 Pro), assist the operator:

- **Migration Planner** — produces a readable migration plan, an ordering rationale, and a list of identified risks for the operator to review before approving.
- **Operator Assistant** — answers free-text operational questions about migrations and the audit log.
- **RCA Assistant** — for a failed migration, reads the audit trail, locates the failure, names the MQ reason code, and produces a structured diagnosis with suggested checks.

The agents **inform**; they do not act. Every state-changing action — provisioning, rewiring, draining, rollback — is performed by deterministic engines. This separation is what keeps the system both intelligent and predictable.

---

## 8. When a migration fails

The failure path is not an afterthought — it is half of the state machine. Walking one failure end to end:

1. **A step fails.** Suppose, during a migration, the target queue manager becomes unreachable while the bridge is being validated. The engine detects the failure at that transition.
2. **The engine rolls back — automatically, and only this application.** The migration transitions to `ROLLING_BACK`. The rollback engine reads this application's audit entries, walks them in reverse Lamport order, and applies the inverse MQSC for each completed step — restoring the source queues, removing the bridge objects. Applications that are already `COMPLETED` or still `PLANNED` are never touched.
3. **The migration settles at `ROLLED_BACK`.** The application is back on its original source queue manager, fully operational. The failure — what failed, when, in what causal order — is captured in the audit log.
4. **The RCA Assistant diagnoses.** It reads the Lamport-ordered audit trail for the failed migration, locates the failure event, identifies the MQ reason code, and produces a structured diagnosis with suggested checks. The operator can also ask it free-text questions about the migration.
5. **The operator restarts.** Once the underlying issue is resolved, the operator restarts: the migration record recycles to `PLANNED` and the idempotent forward path runs again from the start.

If the rollback itself cannot complete, the migration settles at the terminal `ROLLBACK_FAILED` state, which explicitly signals that human recovery is required rather than silently leaving an ambiguous state.

---

## 9. What an operator sees

The UI control plane has four areas:

- **Dashboard** — the estate at a glance: topology, queue manager, audit, and migration counts.
- **Migrations** — per-application state, step-by-step history, the Lamport-ordered audit timeline, the Little's Law drain prediction, the Operator Assistant, and the Markov reliability panel.
- **Topology Viz** — the source and target topologies side by side, with the blast-radius view showing exactly which queues a migration touches.
- **RCA** — the RCA Assistant's diagnosis of a failed migration, a free-text question box, and evidence downloads.

---

## 10. How this maps to the hackathon scope

The submission is intended to demonstrate the following. Each scope item and where IntelliAI 2.0 delivers it:

| Scope item | Where it is delivered |
|---|---|
| Source-topology provisioning through the BCL only | The provisioning engine deploys queue manager pods via the Kubernetes API and realizes their MQ objects via MQSC. The BCL is the only component with cluster or MQ access; the UI has none. |
| Incremental migration from shared to application-dedicated queue managers | Each application migrates independently through its own 12-state machine — one application at a time, never a batch. |
| Transparent rewiring — no producer/consumer connection changes | The cutover swaps an application's `QLOCAL` for a `QREMOTE` alias of the same name over an XMITQ bridge. Connection string and queue name are unchanged; the application does not reconnect. |
| Validation before, during, and after migration | The state machine includes three explicit validation gates — `VALIDATING_PRE`, `VALIDATING_DURING`, `VALIDATING_POST` — and a test-message-flow capability (`amqsput` → poll depth → `amqsget`) proves a real message traverses the path. |
| Automated rollback on failed migration or failed validation | A failure at any step transitions the application to `ROLLING_BACK`; the rollback engine reverses its completed steps in reverse Lamport order, scoped to that application alone. |
| Production-quality operability | Separate liveness/readiness endpoints, structured JSON logging, Prometheus metrics, OpenTelemetry instrumentation, and per-migration evidence export. |

---

## 11. Results

IntelliAI 2.0 was exercised against a real fleet — **16 IBM MQ 9.4.5 queue managers** (9 shared source, 7 dedicated targets) on OpenShift namespace `roco-dev` — and migrated **7 applications** from shared to dedicated queue managers. The provisioning, migration, validation, and rollback evidence is in [`artifacts/output/`](../output/).

---

## 12. Honest limits

Stated plainly, because being precise about what the system does *not* do is part of earning trust:

**On what is proven versus predicted:**

- The migration state machine uses a formal next-state discipline, but the project **does not run a model checker** and does not claim machine-checked correctness.
- The Markov reliability figure is a **model** computed from the transition structure; it is shown alongside — never merged with — an empirical estimate from the real audit log.
- The Little's Law drain time is a **prediction**; the actual drain is always confirmed by polling depth to zero.

**On operational scope:**

- The BCL runs as a single instance. High availability of the control plane itself is out of scope for this submission; the audit log in SQLite means a restarted BCL can recover migration state, and the idempotent forward path allows a migration to be safely re-run.
- The system operates within a single OpenShift namespace.
- Cross-application message dependencies — for example shared reply-to relationships that span applications — are out of scope.

The system is precise about its boundaries. That precision is a deliberate part of the design, not a gap in it.

---

*For the full technical detail — components, the data model, the API surface, the mathematics — see [Architecture](./ARCHITECTURE.md).*

*Team intelliAI2DotO — IBM MQ API Hackathon 2026, Phase 2.*
