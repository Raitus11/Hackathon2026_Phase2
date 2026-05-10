"""Audit log query endpoints."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bcl.db.session import get_session
from bcl.models.api import AuditEntryOut, AuditPage
from bcl.models.orm import AuditLog, AuditOperation

router = APIRouter(prefix="/audit", tags=["audit"])


@router.get("", response_model=AuditPage,
            summary="Paged listing of audit log entries")
async def list_audit(
    session: Annotated[AsyncSession, Depends(get_session)],
    cursor: Annotated[int | None, Query(description="Lamport clock value to fetch entries strictly less than")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    app_id: Annotated[str | None, Query()] = None,
    operation: Annotated[AuditOperation | None, Query()] = None,
    correlation_id: Annotated[str | None, Query()] = None,
    include_total: Annotated[bool, Query()] = False,
) -> AuditPage:
    stmt = select(AuditLog).order_by(AuditLog.lamport_clock.desc())
    if cursor is not None:
        stmt = stmt.where(AuditLog.lamport_clock < cursor)
    if app_id is not None:
        stmt = stmt.where(AuditLog.app_id == app_id)
    if operation is not None:
        stmt = stmt.where(AuditLog.operation == operation)
    if correlation_id is not None:
        stmt = stmt.where(AuditLog.correlation_id == correlation_id)
    stmt = stmt.limit(limit + 1)
    rows = (await session.execute(stmt)).scalars().all()
    has_more = len(rows) > limit
    page = list(rows[:limit])
    next_cursor: int | None = None
    if has_more and page:
        next_cursor = page[-1].lamport_clock
    total: int | None = None
    if include_total:
        count_stmt = select(func.count()).select_from(AuditLog)
        if app_id is not None:
            count_stmt = count_stmt.where(AuditLog.app_id == app_id)
        if operation is not None:
            count_stmt = count_stmt.where(AuditLog.operation == operation)
        if correlation_id is not None:
            count_stmt = count_stmt.where(AuditLog.correlation_id == correlation_id)
        total = (await session.execute(count_stmt)).scalar_one()
    return AuditPage(
        entries=[
            AuditEntryOut(
                id=r.id, lamport_clock=r.lamport_clock, wall_clock=r.wall_clock,
                correlation_id=r.correlation_id, actor=r.actor, operation=r.operation,
                app_id=r.app_id, qm_name=r.qm_name,
                request_payload=r.request_payload, response_payload=r.response_payload,
                state_before=r.state_before, state_after=r.state_after,
                success=r.success, error_message=r.error_message,
                duration_ms=r.duration_ms, is_rollback=r.is_rollback,
            )
            for r in page
        ],
        next_cursor=next_cursor,
        total_count=total,
    )


@router.get("/{lamport}", response_model=AuditEntryOut,
            summary="Fetch one audit entry by Lamport timestamp")
async def get_audit_entry(
    lamport: int,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> AuditEntryOut:
    result = await session.execute(
        select(AuditLog).where(AuditLog.lamport_clock == lamport)
    )
    row = result.scalar_one_or_none()
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No audit entry with lamport_clock={lamport}",
        )
    return AuditEntryOut(
        id=row.id, lamport_clock=row.lamport_clock, wall_clock=row.wall_clock,
        correlation_id=row.correlation_id, actor=row.actor, operation=row.operation,
        app_id=row.app_id, qm_name=row.qm_name,
        request_payload=row.request_payload, response_payload=row.response_payload,
        state_before=row.state_before, state_after=row.state_after,
        success=row.success, error_message=row.error_message,
        duration_ms=row.duration_ms, is_rollback=row.is_rollback,
    )
