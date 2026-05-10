"""Pydantic schemas for the BCL REST API.

Design (Priya + Marcus):
    - Schemas are SEPARATE from ORM models. Same model on the wire and in the
      DB couples API evolution to schema migrations; we don't want that.
    - All schemas are immutable (`model_config = ConfigDict(frozen=True)`)
      except for builders explicitly used as input.
    - Datetime fields serialize as ISO 8601 with timezone.
    - Enum values serialize as strings (not their integer position).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from bcl.models.orm import (
    AgentName,
    AuditOperation,
    ChannelStatus,
    ChannelType,
    MigrationState,
    QueueType,
    TopologyKind,
    ValidationKind,
    ValidationOutcome,
)


# ─────────────────────────────────────────────────────────────────────────
# Base config
# ─────────────────────────────────────────────────────────────────────────


class _ImmutableModel(BaseModel):
    """Base for response models — immutable by default."""

    model_config = ConfigDict(
        frozen=True,
        from_attributes=True,
        ser_json_timedelta="iso8601",
    )


class _MutableModel(BaseModel):
    """Base for input models — mutable, validates on assignment."""

    model_config = ConfigDict(
        validate_assignment=True,
        extra="forbid",   # reject unknown fields — fail loud, fail early
    )


# ─────────────────────────────────────────────────────────────────────────
# Naming validators (Marcus: enforce IBM MQ name limits at the schema
# layer; the guardrails module enforces enterprise patterns on top)
# ─────────────────────────────────────────────────────────────────────────


def _validate_mq_name(v: str, *, max_len: int = 48) -> str:
    """IBM MQ object name validator. 1-N uppercase chars, digits, dots, slashes,
    underscores, percent. Must start with a letter."""
    if not v:
        raise ValueError("MQ name must not be empty")
    if len(v) > max_len:
        raise ValueError(f"MQ name exceeds {max_len} characters: {v}")
    allowed = set("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._/%")
    if not v[0].isalpha() or not v[0].isupper():
        raise ValueError(f"MQ name must start with an uppercase letter: {v}")
    bad = [c for c in v if c not in allowed]
    if bad:
        raise ValueError(f"MQ name contains invalid characters {bad}: {v}")
    return v


# ─────────────────────────────────────────────────────────────────────────
# Application
# ─────────────────────────────────────────────────────────────────────────


class ApplicationOut(_ImmutableModel):
    app_id: str
    app_name: str
    neighbourhood: str
    created_at: datetime


# ─────────────────────────────────────────────────────────────────────────
# Topology
# ─────────────────────────────────────────────────────────────────────────


class FlowSpec(_MutableModel):
    """One row from the source/target CSV — one application-pair flow.

    The schema preserves the input CSV's structure exactly, including the
    typo `consumer_neighnourhood` (extra 'n') — we don't silently fix
    upstream data.
    """

    flow_type: Literal["Local", "Remote"]
    producer_app_id: str
    producer_app_name: str
    producer_neighbourhood: str
    producer_queue_manager: str
    producer_queue_name: str
    producer_queue_type: Literal["Local", "Remote"]
    transmit_queue_name: str | None = None
    channel_name: str | None = None
    consumer_app_id: str
    consumer_app_name: str
    consumer_neighnourhood: str   # sic — preserved from CSV
    consumer_queue_manager: str
    consumer_queue_name: str
    consumer_queue_type: Literal["Local", "Remote"]

    @field_validator(
        "producer_queue_manager",
        "consumer_queue_manager",
        "producer_queue_name",
        "consumer_queue_name",
    )
    @classmethod
    def _check_mq_name(cls, v: str) -> str:
        return _validate_mq_name(v)


class TopologySpec(_MutableModel):
    """The full topology spec — what gets POSTed to /topologies."""

    name: str = Field(min_length=1, max_length=64)
    kind: TopologyKind
    flows: list[FlowSpec] = Field(min_length=1)

    @field_validator("flows")
    @classmethod
    def _consistent_flow_types(cls, flows: list[FlowSpec]) -> list[FlowSpec]:
        """flow_type must match same-QM (Local) vs different-QM (Remote)."""
        for f in flows:
            same_qm = f.producer_queue_manager == f.consumer_queue_manager
            if f.flow_type == "Local" and not same_qm:
                raise ValueError(
                    f"flow_type=Local requires producer_qm == consumer_qm "
                    f"({f.producer_queue_manager} != {f.consumer_queue_manager})"
                )
            if f.flow_type == "Remote" and same_qm:
                raise ValueError(
                    f"flow_type=Remote requires producer_qm != consumer_qm "
                    f"({f.producer_queue_manager} == {f.consumer_queue_manager})"
                )
            if f.flow_type == "Remote":
                if not f.transmit_queue_name or not f.channel_name:
                    raise ValueError(
                        "Remote flow requires transmit_queue_name and channel_name"
                    )
        return flows


class QueueManagerOut(_ImmutableModel):
    id: int
    qm_name: str
    pod_name: str | None
    service_name: str | None
    listener_port: int
    web_port: int
    dlq_name: str
    deployed_at: datetime | None
    is_ready: bool


class TopologyOut(_ImmutableModel):
    id: int
    name: str
    kind: TopologyKind
    spec: dict[str, Any]
    created_at: datetime
    queue_managers: list[QueueManagerOut] = Field(default_factory=list)


# ─────────────────────────────────────────────────────────────────────────
# Migration
# ─────────────────────────────────────────────────────────────────────────


class MigrationStepOut(_ImmutableModel):
    id: int
    step_index: int
    audit_op: AuditOperation
    description: str
    payload: dict[str, Any]
    rollback_payload: dict[str, Any] | None
    started_at: datetime | None
    completed_at: datetime | None
    succeeded: bool | None
    error_message: str | None


class MigrationOut(_ImmutableModel):
    id: int
    app_id: str
    state: MigrationState
    plan: dict[str, Any] | None
    started_at: datetime | None
    completed_at: datetime | None
    version: int
    steps: list[MigrationStepOut] = Field(default_factory=list)


class MigrationPlanRequest(_MutableModel):
    """POST /migrations — request a plan for migrating one app."""

    app_id: str = Field(min_length=1, max_length=64)
    source_topology_name: str
    target_topology_name: str


class MigrationExecuteRequest(_MutableModel):
    """POST /migrations/{id}/execute — operator-confirmed go-ahead."""

    operator: str = Field(min_length=1, max_length=64)
    """Identity of the human operator approving execution. Audit-logged."""


# ─────────────────────────────────────────────────────────────────────────
# Validation
# ─────────────────────────────────────────────────────────────────────────


class ValidationRunOut(_ImmutableModel):
    id: int
    migration_id: int
    migration_step_id: int | None
    kind: ValidationKind
    phase: Literal["PRE", "DURING", "POST"]
    outcome: ValidationOutcome
    evidence: dict[str, Any]
    started_at: datetime
    completed_at: datetime


# ─────────────────────────────────────────────────────────────────────────
# Rollback
# ─────────────────────────────────────────────────────────────────────────


class RollbackRequest(_MutableModel):
    """POST /migrations/{id}/rollback — manual rollback trigger.

    Automatic rollback (validation-failure-driven) does not use this path;
    it's emitted internally by the migration engine.
    """

    operator: str = Field(min_length=1, max_length=64)
    reason: str = Field(min_length=1, max_length=1024)


# ─────────────────────────────────────────────────────────────────────────
# Audit
# ─────────────────────────────────────────────────────────────────────────


class AuditEntryOut(_ImmutableModel):
    id: int
    lamport_clock: int
    wall_clock: datetime
    correlation_id: str
    actor: str
    operation: AuditOperation
    app_id: str | None
    qm_name: str | None
    request_payload: dict[str, Any] | None
    response_payload: dict[str, Any] | None
    state_before: dict[str, Any] | None
    state_after: dict[str, Any] | None
    success: bool
    error_message: str | None
    duration_ms: int | None
    is_rollback: bool


class AuditPage(_ImmutableModel):
    entries: list[AuditEntryOut]
    next_cursor: int | None
    total_count: int | None


# ─────────────────────────────────────────────────────────────────────────
# Health
# ─────────────────────────────────────────────────────────────────────────


class HealthOut(_ImmutableModel):
    status: Literal["healthy", "degraded", "unhealthy"]
    bcl_version: str
    db_reachable: bool
    k8s_reachable: bool
    mq_reachable_count: int
    mq_total_count: int
    lamport_clock: int


# ─────────────────────────────────────────────────────────────────────────
# Agent — Operator Assistant chat endpoints
# ─────────────────────────────────────────────────────────────────────────


class ChatRequest(_MutableModel):
    """POST /chat — user message to the Operator Assistant."""

    message: str = Field(min_length=1, max_length=4000)
    session_id: str = Field(min_length=1, max_length=64)


class ChatCitation(_ImmutableModel):
    """A citation pinned to a UI element or audit entry."""

    kind: Literal["AUDIT", "MIGRATION", "QM", "TOPOLOGY", "METRIC"]
    ref_id: str
    label: str
    """Human-readable label, e.g. 'Audit LC=4521', 'Migration #3'."""

    ui_anchor: str | None = None
    """Optional UI selector for highlight, e.g. 'tab:audit;row:4521'."""


class ChatChunk(_ImmutableModel):
    """One streamed chunk from the assistant. Sent over SSE."""

    kind: Literal["TOKEN", "CITATION", "TOOL_CALL", "DONE", "ERROR"]
    content: str | None = None
    citation: ChatCitation | None = None
    tool_name: str | None = None
    error: str | None = None


# ─────────────────────────────────────────────────────────────────────────
# Evidence
# ─────────────────────────────────────────────────────────────────────────


class EvidenceBundleOut(_ImmutableModel):
    id: int
    migration_id: int
    storage_path: str
    contents: list[dict[str, Any]]
    created_at: datetime


# ─────────────────────────────────────────────────────────────────────────
# Errors
# ─────────────────────────────────────────────────────────────────────────


class ErrorDetail(_ImmutableModel):
    code: str
    message: str
    field: str | None = None
    context: dict[str, Any] | None = None


class ErrorResponse(_ImmutableModel):
    """Standard error envelope. All non-2xx responses use this shape."""

    error: str
    """Short error category, e.g. 'guardrail_violation', 'not_found'."""

    correlation_id: str
    details: list[ErrorDetail] = Field(default_factory=list)


__all__ = [
    "ApplicationOut",
    "AuditEntryOut",
    "AuditPage",
    "ChatChunk",
    "ChatCitation",
    "ChatRequest",
    "ErrorDetail",
    "ErrorResponse",
    "EvidenceBundleOut",
    "FlowSpec",
    "HealthOut",
    "MigrationExecuteRequest",
    "MigrationOut",
    "MigrationPlanRequest",
    "MigrationStepOut",
    "QueueManagerOut",
    "RollbackRequest",
    "TopologyOut",
    "TopologySpec",
    "ValidationRunOut",
]
