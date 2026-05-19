"""SQLAlchemy ORM models — the state shape of the BCL.

Design notes:

* Every state-changing operation produces an AuditLog entry with a Lamport
  timestamp. Lamport ordering is the source of truth for causality; wall-clock
  is for operator convenience only.

* AuditLog is APPEND-ONLY. There are no UPDATE or DELETE paths in code.
  An Alembic migration is the only thing that can touch a written row.
  This is a critical invariant for the system-of-record property.

* Migration state is the second source of truth. AuditLog records what
  happened; Migration records the current state of each app's migration.
  These two must be kept consistent — the audit log is the ground truth,
  Migration is a materialized projection.

* QM, Queue, Channel state is intent-based, not live. We persist what the
  BCL TOLD MQ to do (via MQSC). Live MQ state is queried on demand from
  the QM pods themselves and rendered in the UI; we do not try to mirror
  live MQ state into our DB.

* Naming follows IBM MQ enterprise patterns. Constraints in the guardrails
  module enforce these on write; the schema does not.

* All timestamps are timezone-aware UTC. `datetime.now(UTC)` not `utcnow()`.
"""

from __future__ import annotations

import enum
from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Enum as SAEnum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    """Declarative base for all ORM models."""

    type_annotation_map = {
        dict[str, Any]: JSON,
        list[Any]: JSON,
    }


# ─────────────────────────────────────────────────────────────────────────
# Enums
# ─────────────────────────────────────────────────────────────────────────


class TopologyKind(str, enum.Enum):
    """Source vs target topology — the two API layers per FAQ Q6/Q7."""

    SOURCE = "SOURCE"
    TARGET = "TARGET"


class QueueType(str, enum.Enum):
    """MQ queue object types we care about for migration."""

    LOCAL = "LOCAL"          # QLOCAL
    REMOTE = "REMOTE"        # QREMOTE
    TRANSMIT = "TRANSMIT"    # QLOCAL with USAGE(XMITQ)
    DLQ = "DLQ"              # the QM-wide dead-letter queue
    MODEL = "MODEL"          # not used in migration but allowed in schema


class ChannelType(str, enum.Enum):
    """Channel types relevant to migration."""

    SDR = "SDR"              # sender, on producer-side QM
    RCVR = "RCVR"            # receiver, on consumer-side QM
    SVRCONN = "SVRCONN"      # client-to-QM channel, used by test apps


class ChannelStatus(str, enum.Enum):
    """Live channel status — captured in audit only when probed."""

    INACTIVE = "INACTIVE"
    BINDING = "BINDING"
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    STOPPING = "STOPPING"
    RETRYING = "RETRYING"
    STOPPED = "STOPPED"
    UNKNOWN = "UNKNOWN"


class MigrationState(str, enum.Enum):
    """Per-app migration state machine.

    Forward path:
        PLANNED → AWAITING_APPROVAL → PROVISIONING_TARGET_QM → VALIDATING_PRE
        → REWIRING → DRAIN_WAIT → VALIDATING_DURING → DRAINING_SOURCE
        → VALIDATING_POST → COMPLETED

    Human approval gate:
        After the planner runs, the engine parks the migration in
        AWAITING_APPROVAL and stops. An operator reviews the plan +
        risk brief + go/no-go score, then POSTs /approve (→ resume the
        forward path) or /abort (→ ROLLING_BACK → ROLLED_BACK).

    Failure / rollback:
        any-state → ROLLING_BACK → ROLLED_BACK
        ROLLBACK_FAILED is terminal — requires human intervention.
    """

    PLANNED = "PLANNED"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    PROVISIONING_TARGET_QM = "PROVISIONING_TARGET_QM"
    VALIDATING_PRE = "VALIDATING_PRE"
    REWIRING = "REWIRING"
    DRAIN_WAIT = "DRAIN_WAIT"
    VALIDATING_DURING = "VALIDATING_DURING"
    DRAINING_SOURCE = "DRAINING_SOURCE"
    VALIDATING_POST = "VALIDATING_POST"
    COMPLETED = "COMPLETED"
    ROLLING_BACK = "ROLLING_BACK"
    ROLLED_BACK = "ROLLED_BACK"
    ROLLBACK_FAILED = "ROLLBACK_FAILED"


class ValidationKind(str, enum.Enum):
    """The 4 functional tests per FAQ Q15."""

    CONNECTIVITY = "CONNECTIVITY"
    MESSAGE_FLOW = "MESSAGE_FLOW"
    FUNCTIONAL = "FUNCTIONAL"
    APP_RECONNECT = "APP_RECONNECT"


class ValidationOutcome(str, enum.Enum):
    PASS = "PASS"
    WARN = "WARN"
    FAIL = "FAIL"


class AuditOperation(str, enum.Enum):
    """Operation taxonomy on the audit log.

    Granular enough that the rollback engine can synthesize an inverse
    MQSC command from the operation alone, given the operation's payload.
    """

    # Topology lifecycle
    TOPOLOGY_CREATED = "TOPOLOGY_CREATED"
    TOPOLOGY_DELETED = "TOPOLOGY_DELETED"

    # QM lifecycle (K8s-level, not MQ-level)
    PROVISION_STARTED = "PROVISION_STARTED"
    PROVISION_COMPLETED = "PROVISION_COMPLETED"
    PROVISION_FAILED = "PROVISION_FAILED"
    QM_PVC_CREATED = "QM_PVC_CREATED"
    QM_SECRET_CREATED = "QM_SECRET_CREATED"
    QM_DEPLOYED = "QM_DEPLOYED"
    QM_SERVICE_CREATED = "QM_SERVICE_CREATED"
    QM_READY = "QM_READY"
    QM_DELETED = "QM_DELETED"

    # MQ object lifecycle (one MQSC command per entry)
    MQSC_DEFINE_QLOCAL = "MQSC_DEFINE_QLOCAL"
    MQSC_DEFINE_QREMOTE = "MQSC_DEFINE_QREMOTE"
    MQSC_DEFINE_QXMIT = "MQSC_DEFINE_QXMIT"        # XMITQ
    MQSC_DEFINE_CHANNEL_SDR = "MQSC_DEFINE_CHANNEL_SDR"
    MQSC_DEFINE_CHANNEL_RCVR = "MQSC_DEFINE_CHANNEL_RCVR"
    MQSC_DEFINE_CHANNEL_SVRCONN = "MQSC_DEFINE_CHANNEL_SVRCONN"
    MQSC_DEFINE_CHLAUTH = "MQSC_DEFINE_CHLAUTH"
    MQSC_ALTER_QMGR = "MQSC_ALTER_QMGR"             # e.g. setting DEADQ
    MQSC_DELETE_QLOCAL = "MQSC_DELETE_QLOCAL"
    MQSC_DELETE_QREMOTE = "MQSC_DELETE_QREMOTE"
    MQSC_DELETE_QXMIT = "MQSC_DELETE_QXMIT"
    MQSC_DELETE_CHANNEL = "MQSC_DELETE_CHANNEL"
    MQSC_START_CHANNEL = "MQSC_START_CHANNEL"
    MQSC_STOP_CHANNEL = "MQSC_STOP_CHANNEL"

    # Migration lifecycle
    MIGRATION_PLANNED = "MIGRATION_PLANNED"
    MIGRATION_STATE_TRANSITION = "MIGRATION_STATE_TRANSITION"
    MIGRATION_STEP_STARTED = "MIGRATION_STEP_STARTED"
    MIGRATION_STEP_COMPLETED = "MIGRATION_STEP_COMPLETED"
    MIGRATION_STEP_FAILED = "MIGRATION_STEP_FAILED"

    # Human approval gate
    MIGRATION_AWAITING_APPROVAL = "MIGRATION_AWAITING_APPROVAL"
    MIGRATION_APPROVED = "MIGRATION_APPROVED"
    MIGRATION_ABORTED = "MIGRATION_ABORTED"
    PREFLIGHT_RISK_BRIEF = "PREFLIGHT_RISK_BRIEF"

    # Validation
    VALIDATION_RUN = "VALIDATION_RUN"

    # Rollback
    ROLLBACK_INITIATED = "ROLLBACK_INITIATED"
    ROLLBACK_STEP = "ROLLBACK_STEP"
    ROLLBACK_COMPLETED = "ROLLBACK_COMPLETED"
    ROLLBACK_FAILED = "ROLLBACK_FAILED"

    # Guardrails
    GUARDRAIL_REJECTED = "GUARDRAIL_REJECTED"

    # Agent
    AGENT_INVOCATION = "AGENT_INVOCATION"


class AgentName(str, enum.Enum):
    """The agents in our scope.

    All are read-only over BCL state and audit-logged on every
    invocation. None has a write tool — destructive actions stay with
    the deterministic engines and the operator. Each has a deterministic
    fallback so the system degrades gracefully without the LLM.
    """

    MIGRATION_PLANNER = "MIGRATION_PLANNER"
    OPERATOR_ASSISTANT = "OPERATOR_ASSISTANT"
    PREFLIGHT_AUDITOR = "PREFLIGHT_AUDITOR"
    """Pre-Flight Risk Auditor. Runs once, between planning and the
    human approval gate. Reads the deterministic blast-radius analysis
    + the planner's plan, and produces a structured risk brief: the
    non-obvious hazards an operator should weigh before approving.
    Advisory only — it cannot start, stop, or alter a migration. Has a
    deterministic fallback like every other agent."""
    COMPLIANCE_NARRATOR = "COMPLIANCE_NARRATOR"
    """Compliance Narrator. Runs once, when a migration reaches a
    terminal state (COMPLETED or ROLLED_BACK). Reads the migration's
    Lamport-ordered audit trail and produces an evidence-cited Markdown
    narrative for the per-app evidence bundle — the kind of document a
    SOX-style auditor would expect. Read-only; advisory; has a
    deterministic templated fallback."""
    RCA_ASSISTANT = "RCA_ASSISTANT"
    """Root Cause Analysis. Reads the Lamport-ordered audit trail of a
    migration, locates the failure event, names the MQ reason code, and
    produces a structured diagnosis (hypothesis + evidence + suggested
    human checks). Diagnosis only — it never remediates."""


# ─────────────────────────────────────────────────────────────────────────
# Topology / Application
# ─────────────────────────────────────────────────────────────────────────


class Application(Base):
    """An application that produces or consumes messages.

    From the source/target CSV `producer_app_id` / `consumer_app_id`.
    Persisted once per distinct app_id (stripped of `/` for storage).
    """

    __tablename__ = "applications"

    app_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    """Stable identifier from CSV. Slashes preserved (e.g. 'LIY/KW')."""

    app_name: Mapped[str] = mapped_column(String(256), nullable=False)
    neighbourhood: Mapped[str] = mapped_column(String(64), nullable=False)
    """e.g. 'Data & Analytics', 'Core Banking, Mainframe', 'Mainframe'."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    # Reverse relationships
    migrations: Mapped[list[Migration]] = relationship(
        back_populates="application", cascade="all, delete-orphan"
    )


class Topology(Base):
    """A named topology snapshot — source or target."""

    __tablename__ = "topologies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    """e.g. 'source-v1', 'target-v1'."""

    kind: Mapped[TopologyKind] = mapped_column(
        SAEnum(TopologyKind, native_enum=False), nullable=False
    )
    spec: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    """The full topology spec (parsed CSV) as JSON. Source of truth for re-bootstrap."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    queue_managers: Mapped[list[QueueManager]] = relationship(
        back_populates="topology", cascade="all, delete-orphan"
    )


class QueueManager(Base):
    """An IBM MQ queue manager — one OCP pod per QM.

    Source QMs: consolidated by neighbourhood (3 pods).
    Target QMs: one per app (7 pods).
    """

    __tablename__ = "queue_managers"
    __table_args__ = (
        UniqueConstraint("topology_id", "qm_name", name="uq_qm_topology_name"),
        CheckConstraint(
            "qm_name = upper(qm_name)", name="ck_qm_name_uppercase"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    topology_id: Mapped[int] = mapped_column(
        ForeignKey("topologies.id", ondelete="CASCADE"), nullable=False, index=True
    )
    qm_name: Mapped[str] = mapped_column(String(48), nullable=False)
    """IBM MQ name limit is 48 chars. Examples: APPQM_LIY_KW, SRC_QM_DA."""

    pod_name: Mapped[str | None] = mapped_column(String(63), nullable=True)
    """K8s pod name — populated after deployment. Null if not yet deployed."""

    service_name: Mapped[str | None] = mapped_column(String(63), nullable=True)
    """K8s service DNS name — what producers/consumers connect to."""

    listener_port: Mapped[int] = mapped_column(Integer, default=1414, nullable=False)
    web_port: Mapped[int] = mapped_column(Integer, default=9443, nullable=False)

    dlq_name: Mapped[str] = mapped_column(
        String(48), default="SYSTEM.DEAD.LETTER.QUEUE", nullable=False
    )
    """Mandatory per guardrail — every QM has a DLQ. Brief constraint #3."""

    deployed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    is_ready: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    topology: Mapped[Topology] = relationship(back_populates="queue_managers")


class Migration(Base):
    """The migration of a single application from source to target topology.

    There is exactly one Migration row per (application, target_topology) pair.
    """

    __tablename__ = "migrations"
    __table_args__ = (
        UniqueConstraint("app_id", "target_topology_id", name="uq_migration_app_target"),
        Index("ix_migration_state", "state"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    app_id: Mapped[str] = mapped_column(
        ForeignKey("applications.app_id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    source_topology_id: Mapped[int] = mapped_column(
        ForeignKey("topologies.id"), nullable=False
    )
    target_topology_id: Mapped[int] = mapped_column(
        ForeignKey("topologies.id"), nullable=False
    )

    state: Mapped[MigrationState] = mapped_column(
        SAEnum(MigrationState, native_enum=False),
        default=MigrationState.PLANNED,
        nullable=False,
    )

    plan: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    """The Migration Planner agent's output. List of ordered steps + rationale."""

    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Optimistic concurrency — for two-operator races (FAQ Q13)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    application: Mapped[Application] = relationship(back_populates="migrations")
    steps: Mapped[list[MigrationStep]] = relationship(
        back_populates="migration",
        cascade="all, delete-orphan",
        order_by="MigrationStep.step_index",
    )


class MigrationStep(Base):
    """A single step within a Migration — typically one MQSC operation
    or a state-machine transition.

    Steps are ordered. Each step records:
        - what was done (the audit_op + payload)
        - whether it succeeded
        - the inverse action for rollback (the rollback engine
          uses this; without it, rollback is impossible)
    """

    __tablename__ = "migration_steps"
    __table_args__ = (
        UniqueConstraint("migration_id", "step_index", name="uq_step_index"),
        Index("ix_step_status", "migration_id", "succeeded"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    migration_id: Mapped[int] = mapped_column(
        ForeignKey("migrations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    step_index: Mapped[int] = mapped_column(Integer, nullable=False)

    audit_op: Mapped[AuditOperation] = mapped_column(
        SAEnum(AuditOperation, native_enum=False), nullable=False
    )
    description: Mapped[str] = mapped_column(Text, nullable=False)

    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    """The forward action's parameters (e.g. MQSC command, target QM, queue name)."""

    rollback_payload: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    """The inverse action's parameters. Computed when the step is created.

    The rollback engine reads this column in reverse-step-index order.
    If a step has no inverse (e.g. some ALTER QMGR), rollback_payload is null
    and the rollback engine logs that the step is non-reversible.
    """

    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    succeeded: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    """null = pending, true = success, false = failed."""

    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    migration: Mapped[Migration] = relationship(back_populates="steps")
    audit_log_id: Mapped[int | None] = mapped_column(
        ForeignKey("audit_log.id"), nullable=True, index=True
    )
    """Pointer to the audit entry that recorded this step's execution."""


# ─────────────────────────────────────────────────────────────────────────
# Validation
# ─────────────────────────────────────────────────────────────────────────


class ValidationRun(Base):
    """A single validation test invocation.

    Multiple ValidationRuns happen per migration step (pre / during / post).
    """

    __tablename__ = "validation_runs"
    __table_args__ = (
        Index("ix_validation_migration", "migration_id"),
        Index("ix_validation_step", "migration_step_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    migration_id: Mapped[int] = mapped_column(
        ForeignKey("migrations.id", ondelete="CASCADE"), nullable=False
    )
    migration_step_id: Mapped[int | None] = mapped_column(
        ForeignKey("migration_steps.id", ondelete="CASCADE"), nullable=True
    )

    kind: Mapped[ValidationKind] = mapped_column(
        SAEnum(ValidationKind, native_enum=False), nullable=False
    )
    phase: Mapped[str] = mapped_column(String(16), nullable=False)
    """'PRE' | 'DURING' | 'POST'."""

    outcome: Mapped[ValidationOutcome] = mapped_column(
        SAEnum(ValidationOutcome, native_enum=False), nullable=False
    )
    evidence: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    """Per-test evidence — message counts, latency samples, error logs, etc."""

    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    completed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


# ─────────────────────────────────────────────────────────────────────────
# Audit log — append-only, Lamport-timestamped
# ─────────────────────────────────────────────────────────────────────────


class AuditLog(Base):
    """Append-only audit log. The system of record.

    Every row is immutable. There are no UPDATE or DELETE paths in code.

    The lamport_clock column is the canonical ordering. Wall-clock is for
    operator convenience only. Lamport is monotonically increasing per BCL
    instance (incremented on every write, advanced past any received clock
    on cross-process events — though we are single-instance for the
    hackathon, so received clocks don't apply).

    This table must not have ON UPDATE triggers, must not be in any
    GRANT/REVOKE that allows UPDATE/DELETE for the BCL role. Schema-level
    immutability is enforced by code review and by integration tests
    that try to UPDATE/DELETE and assert failure.
    """

    __tablename__ = "audit_log"
    __table_args__ = (
        Index("ix_audit_lamport", "lamport_clock", unique=True),
        Index("ix_audit_app", "app_id"),
        Index("ix_audit_op", "operation"),
        Index("ix_audit_correlation", "correlation_id"),
        Index("ix_audit_wall_clock", "wall_clock"),
    )

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
    )

    lamport_clock: Mapped[int] = mapped_column(BigInteger, nullable=False)
    """Lamport timestamp. Monotonically increasing. Source of causal truth."""

    wall_clock: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    correlation_id: Mapped[str] = mapped_column(String(36), nullable=False)
    """UUID per request — propagated through all sub-operations."""

    actor: Mapped[str] = mapped_column(String(64), nullable=False)
    """Who initiated. 'bcl-system', 'operator:<name>', 'agent:MIGRATION_PLANNER', etc."""

    operation: Mapped[AuditOperation] = mapped_column(
        SAEnum(AuditOperation, native_enum=False), nullable=False
    )

    app_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    """The app this event pertains to, if any. Null for system-level events."""

    qm_name: Mapped[str | None] = mapped_column(String(48), nullable=True)

    request_payload: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    """The input to the operation — for state-changing ops, includes MQSC command."""

    response_payload: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    """The output — for MQSC ops, includes the runmqsc response (e.g. AMQ8006I…)."""

    state_before: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    state_after: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

    success: Mapped[bool] = mapped_column(Boolean, nullable=False)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    """For ops that have a measurable duration (MQSC, K8s API, agent invocation)."""

    is_rollback: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    """Marks rollback-emitted entries. Bypasses some forward-direction guardrails
    (per FAQ Q9-equivalent: rollbacks intentionally move toward source state).
    """


# ─────────────────────────────────────────────────────────────────────────
# Agent invocations
# ─────────────────────────────────────────────────────────────────────────


class AgentInvocation(Base):
    """One invocation of one agent. Audit-grade record of all LLM calls."""

    __tablename__ = "agent_invocations"
    __table_args__ = (
        Index("ix_agent_name", "agent_name"),
        Index("ix_agent_correlation", "correlation_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    correlation_id: Mapped[str] = mapped_column(String(36), nullable=False)

    agent_name: Mapped[AgentName] = mapped_column(
        SAEnum(AgentName, native_enum=False), nullable=False
    )

    trigger: Mapped[str] = mapped_column(String(128), nullable=False)
    """What caused the invocation — e.g. 'POST /migrations/plan', 'chat:user-msg'."""

    input_summary: Mapped[str] = mapped_column(Text, nullable=False)
    """Truncated input (first 1000 chars). Full input in input_full."""
    input_full: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)

    output: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    output_text: Mapped[str | None] = mapped_column(Text, nullable=True)

    tools_called: Mapped[list[Any]] = mapped_column(JSON, default=list, nullable=False)
    """List of {tool_name, args, result_summary} for each tool call."""

    model: Mapped[str] = mapped_column(String(64), nullable=False)
    """e.g. 'tachyon:gemini-2.5-pro', 'stub:deterministic'."""

    tokens_in: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tokens_out: Mapped[int | None] = mapped_column(Integer, nullable=True)
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False)

    succeeded: Mapped[bool] = mapped_column(Boolean, nullable=False)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


# ─────────────────────────────────────────────────────────────────────────
# Evidence bundles
# ─────────────────────────────────────────────────────────────────────────


class EvidenceBundle(Base):
    """A per-app evidence bundle assembled at migration completion (or rollback).

    Bundle contents are written as files on disk under /data/evidence/<app>/<id>/
    and zipped on download. This row is the index entry.
    """

    __tablename__ = "evidence_bundles"
    __table_args__ = (Index("ix_evidence_migration", "migration_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    migration_id: Mapped[int] = mapped_column(
        ForeignKey("migrations.id", ondelete="CASCADE"), nullable=False
    )

    storage_path: Mapped[str] = mapped_column(String(512), nullable=False)
    """Local path on the BCL pod's PVC, e.g. /data/evidence/APUMN_GC/42/."""

    contents: Mapped[list[Any]] = mapped_column(JSON, nullable=False)
    """Manifest of files in the bundle: [{filename, type, size_bytes, checksum}]."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


# ─────────────────────────────────────────────────────────────────────────
# Knowledge base — for Operator Assistant agent
# ─────────────────────────────────────────────────────────────────────────


class KnowledgeEntry(Base):
    """A retrievable knowledge entry. Used by the Operator Assistant agent.

    Sources include: ingested MQ docs, internal naming policies, past incidents
    summarized at migration completion. Vector embeddings are NOT stored here —
    we use BM25-only retrieval for the hackathon (vector search adds
    a chroma/pgvector dependency for marginal gain at 7-app scope; cut).
    """

    __tablename__ = "knowledge_entries"
    __table_args__ = (
        Index("ix_kb_kind", "kind"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    """'MQ_DOC', 'POLICY', 'INCIDENT', 'INTERNAL'."""

    title: Mapped[str] = mapped_column(String(256), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    tags: Mapped[list[Any]] = mapped_column(JSON, default=list, nullable=False)
    source_url: Mapped[str | None] = mapped_column(String(512), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


# ─────────────────────────────────────────────────────────────────────────
# Provisioning — tracks /topologies/{id}/provision runs
# ─────────────────────────────────────────────────────────────────────────


class ProvisionState(str, enum.Enum):
    """State machine for a single /topologies/{id}/provision run."""

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    PARTIALLY_COMPLETED = "PARTIALLY_COMPLETED"   # some QMs deployed, some failed


class ProvisionRun(Base):
    """One execution of POST /topologies/{id}/provision.

    Async: the POST returns immediately with a run_id; the run executes
    in the background, updating progress here. Clients poll
    GET /topologies/{id}/provision/{run_id}/status.

    Every K8s resource applied (PVC, Secret, Deployment, Service) is
    audit-logged with this run's correlation_id so the full provenance
    chain is recoverable.
    """

    __tablename__ = "provision_runs"
    __table_args__ = (
        Index("ix_prov_run_topology", "topology_id"),
        Index("ix_prov_run_state", "state"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(36), unique=True, nullable=False)
    """UUID returned to the client for polling."""

    topology_id: Mapped[int] = mapped_column(
        ForeignKey("topologies.id", ondelete="CASCADE"), nullable=False, index=True
    )

    state: Mapped[ProvisionState] = mapped_column(
        SAEnum(ProvisionState, native_enum=False), nullable=False
    )

    qms_total: Mapped[int] = mapped_column(Integer, nullable=False)
    qms_ready: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    qms_failed: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    correlation_id: Mapped[str] = mapped_column(String(36), nullable=False)
    """Same correlation_id appears on every audit log entry for this run."""

    actor: Mapped[str] = mapped_column(String(64), nullable=False)
    """Identity of the operator who initiated the run. Audit traceability."""

    operator_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    """Optional human-friendly message attached at start time."""

    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    """Populated if state in {FAILED, PARTIALLY_COMPLETED}."""

    progress: Mapped[list[Any]] = mapped_column(JSON, default=list, nullable=False)
    """Append-only list of per-QM progress events. Each event is a dict:
    {qm_name, phase, status, timestamp, error?}.
    Phases: PVC_APPLY, SECRET_APPLY, DEPLOYMENT_APPLY, SERVICE_APPLY,
    WAIT_FOR_READY, COMPLETE.
    """


# ─────────────────────────────────────────────────────────────────────────
# MQ object realization — tracks /topologies/{id}/realize-mq-objects runs
# ─────────────────────────────────────────────────────────────────────────


class MqRealizeState(str, enum.Enum):
    """State machine for a single /realize-mq-objects (or teardown) run.

    Same shape as ProvisionState. Separate enum so the two run lifecycles
    can evolve independently without overloaded semantics.
    """

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    PARTIALLY_COMPLETED = "PARTIALLY_COMPLETED"


class MqRealizeRun(Base):
    """One execution of POST or DELETE on /realize-mq-objects.

    Mirrors ProvisionRun in shape (same async-with-polling pattern), but
    tracks MQ-object-level provisioning rather than K8s-resource-level.

    Two directions of the same engine:
      - direction='APPLY'    -> DEFINE QLOCAL/QREMOTE/QXMIT/CHANNEL ...
      - direction='TEARDOWN' -> DELETE QLOCAL/QREMOTE/QXMIT/CHANNEL ...

    Every MQSC command issued is also recorded as one AuditLog row with
    the matching AuditOperation.MQSC_* op, sharing this run's correlation_id.
    """

    __tablename__ = "mq_realize_runs"
    __table_args__ = (
        Index("ix_realize_run_topology", "topology_id"),
        Index("ix_realize_run_state", "state"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(36), unique=True, nullable=False)

    topology_id: Mapped[int] = mapped_column(
        ForeignKey("topologies.id", ondelete="CASCADE"), nullable=False, index=True
    )

    direction: Mapped[str] = mapped_column(String(16), nullable=False)
    """'APPLY' or 'TEARDOWN'. Determines whether commands are DEFINE or DELETE."""

    state: Mapped[MqRealizeState] = mapped_column(
        SAEnum(MqRealizeState, native_enum=False), nullable=False
    )

    # Counters: at the QM granularity, NOT command granularity, so the API
    # response shape matches ProvisionRun closely.
    qms_total: Mapped[int] = mapped_column(Integer, nullable=False)
    qms_completed: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    qms_failed: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # Command-level counters surface the deeper detail.
    commands_total: Mapped[int] = mapped_column(Integer, nullable=False)
    commands_applied: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    commands_skipped_idempotent: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False
    )
    """Commands that ran but hit AMQ8350/AMQ8013/etc — already-exists during
    APPLY, or already-absent during TEARDOWN. Counted as success."""
    commands_failed: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    correlation_id: Mapped[str] = mapped_column(String(36), nullable=False)
    actor: Mapped[str] = mapped_column(String(64), nullable=False)
    operator_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    progress: Mapped[list[Any]] = mapped_column(JSON, default=list, nullable=False)
    """Append-only list of per-QM events:
    {qm_name, phase, status, timestamp, command_count?, error?, warnings?}.
    Phases: PLAN_DERIVED, APPLYING, APPLIED, FAILED, COMPLETE.
    """

    derived_plans_summary: Mapped[dict[str, Any] | None] = mapped_column(
        JSON, nullable=True
    )
    """Snapshot of every QM's plan summary (qm_name -> plan.to_summary_dict()).
    Captured at run start so 'what was supposed to happen' is recoverable
    even after the run completes."""


__all__ = [
    "Base",
    # enums
    "TopologyKind",
    "QueueType",
    "ChannelType",
    "ChannelStatus",
    "MigrationState",
    "ValidationKind",
    "ValidationOutcome",
    "AuditOperation",
    "AgentName",
    "ProvisionState",
    "MqRealizeState",
    # tables
    "Application",
    "Topology",
    "QueueManager",
    "Migration",
    "MigrationStep",
    "ValidationRun",
    "AuditLog",
    "AgentInvocation",
    "EvidenceBundle",
    "KnowledgeEntry",
    "ProvisionRun",
    "MqRealizeRun",
]
