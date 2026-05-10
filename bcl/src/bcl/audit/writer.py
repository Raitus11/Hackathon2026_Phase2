"""Audit log writer.

Every state-changing operation in the BCL emits exactly one AuditLog row
through `write_audit_entry`. Every row carries Lamport clock, wall-clock,
correlation ID, actor, operation, payloads, before/after state, success
flag, error message, duration.

This is what makes the BCL the system of record (Marcus's Moat 3).
The audit log is APPEND-ONLY at the application layer.
"""

from __future__ import annotations

import uuid
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from bcl.audit.lamport import LamportClock
from bcl.config import get_settings
from bcl.models.orm import AuditLog, AuditOperation

_correlation_id_var: ContextVar[str | None] = ContextVar(
    "correlation_id", default=None
)
_actor_var: ContextVar[str] = ContextVar("actor", default="bcl-system")


def set_correlation_id(value: str) -> None:
    _correlation_id_var.set(value)


def set_actor(value: str) -> None:
    _actor_var.set(value)


def get_correlation_id() -> str:
    cid = _correlation_id_var.get()
    if cid is None:
        cid = str(uuid.uuid4())
        _correlation_id_var.set(cid)
    return cid


def get_actor() -> str:
    return _actor_var.get()


def _truncate_payload(
    payload: dict[str, Any] | None, max_bytes: int
) -> dict[str, Any] | None:
    if payload is None:
        return None
    import json
    encoded = json.dumps(payload, default=str)
    if len(encoded.encode("utf-8")) <= max_bytes:
        return payload
    return {
        "_truncated": True,
        "_original_size_bytes": len(encoded.encode("utf-8")),
        "_max_bytes": max_bytes,
        "_summary": encoded[:1000],
    }


async def write_audit_entry(
    session: AsyncSession,
    *,
    operation: AuditOperation,
    success: bool,
    actor: str | None = None,
    correlation_id: str | None = None,
    app_id: str | None = None,
    qm_name: str | None = None,
    request_payload: dict[str, Any] | None = None,
    response_payload: dict[str, Any] | None = None,
    state_before: dict[str, Any] | None = None,
    state_after: dict[str, Any] | None = None,
    error_message: str | None = None,
    duration_ms: int | None = None,
    is_rollback: bool = False,
) -> AuditLog:
    """Append an entry to the audit log. Returns the persisted row."""
    settings = get_settings()
    clock = LamportClock.instance()

    if not clock._bootstrapped:
        await clock.bootstrap(session)

    lamport = await clock.tick()

    entry = AuditLog(
        lamport_clock=lamport,
        wall_clock=datetime.now(UTC),
        correlation_id=correlation_id or get_correlation_id(),
        actor=actor or get_actor(),
        operation=operation,
        app_id=app_id,
        qm_name=qm_name,
        request_payload=_truncate_payload(request_payload, settings.audit_max_payload_bytes),
        response_payload=_truncate_payload(response_payload, settings.audit_max_payload_bytes),
        state_before=_truncate_payload(state_before, settings.audit_max_payload_bytes),
        state_after=_truncate_payload(state_after, settings.audit_max_payload_bytes),
        success=success,
        error_message=error_message,
        duration_ms=duration_ms,
        is_rollback=is_rollback,
    )
    session.add(entry)
    await session.flush()
    return entry
