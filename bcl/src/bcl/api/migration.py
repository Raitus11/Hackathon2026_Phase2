"""Migration REST endpoints.

POST   /migrations                      start a per-app migration (async, 202)
GET    /migrations/{id}                 fetch one migration (with steps)
GET    /migrations                      list migrations (filter by app/target)
POST   /migrations/{id}/rollback        manual rollback (async, 202)
GET    /migrations/{id}/drain           live drain progress for the running migration
GET    /migrations/{id}/audit           audit-log entries scoped to this migration
GET    /migrations/{id}/plan            the persisted planner output

The POST endpoints return 202 immediately with the Migration row;
the actual work runs in a background task. Clients poll
GET /migrations/{id} until `state in {COMPLETED, ROLLED_BACK,
ROLLBACK_FAILED}`.

Mirrors the existing provisioning + realize endpoints in shape so the
UI's polling code is uniform across all three workflows.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bcl.db.session import get_session, get_session_factory
from bcl.migration import engine as migration_engine
from bcl.migration import states
from bcl.models.api import (
    MigrationOut,
    MigrationPlanRequest,
    MigrationStepOut,
    RollbackRequest,
)
from bcl.models.orm import (
    AuditLog,
    Migration,
    MigrationState,
    MigrationStep,
)
from bcl.rollback import engine as rollback_engine

router = APIRouter(prefix="/migrations", tags=["migration"])


# ─────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────


def _to_migration_out(m: Migration) -> MigrationOut:
    """Build the API response shape from an ORM row. The Migration must
    have its `steps` relationship loaded (use selectinload at the query
    site).
    """
    return MigrationOut(
        id=m.id,
        app_id=m.app_id,
        state=m.state,
        plan=m.plan,
        started_at=m.started_at,
        completed_at=m.completed_at,
        version=m.version,
        steps=[
            MigrationStepOut(
                id=s.id,
                step_index=s.step_index,
                audit_op=s.audit_op,
                description=s.description,
                payload=s.payload,
                rollback_payload=s.rollback_payload,
                started_at=s.started_at,
                completed_at=s.completed_at,
                succeeded=s.succeeded,
                error_message=s.error_message,
            )
            for s in (m.steps or [])
        ],
    )


# ─────────────────────────────────────────────────────────────────────────
# POST /migrations — plan + start the migration
# ─────────────────────────────────────────────────────────────────────────


@router.post(
    "",
    response_model=MigrationOut,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Plan and start a migration for one application",
    description=(
        "Creates a Migration row for the given app + (source, target) "
        "topology pair, runs the Migration Planner agent, persists the "
        "plan, and schedules the state-machine in the background. "
        "Returns 202 immediately. Poll GET /migrations/{id} for status.\n\n"
        "If a non-terminal Migration for this (app_id, target_topology) "
        "pair already exists, returns 409. If a terminal Migration "
        "exists, the engine recycles it (resets to PLANNED, bumps "
        "`version`, clears child steps) — this supports 'rollback then "
        "re-migrate' without an Alembic change."
    ),
)
async def plan_and_start_migration(
    body: MigrationPlanRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[str, Query(min_length=1, max_length=64)] = "operator:anon",
) -> MigrationOut:
    try:
        migration = await migration_engine.start_migration_run(
            session,
            app_id=body.app_id,
            source_topology_name=body.source_topology_name,
            target_topology_name=body.target_topology_name,
            actor=actor,
            session_factory=get_session_factory(),
        )
    except LookupError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc),
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(exc),
        ) from exc

    # Re-fetch with relationships loaded for the response.
    out = await migration_engine.get_migration(session, migration.id)
    if out is None:  # shouldn't happen — we just inserted
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="migration row vanished post-insert",
        )
    return _to_migration_out(out)


# ─────────────────────────────────────────────────────────────────────────
# GET /migrations/{id}
# ─────────────────────────────────────────────────────────────────────────


@router.get(
    "/{migration_id}",
    response_model=MigrationOut,
    summary="Fetch one migration (with steps + plan)",
)
async def get_migration(
    migration_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> MigrationOut:
    m = await migration_engine.get_migration(session, migration_id)
    if m is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"migration {migration_id} not found",
        )
    return _to_migration_out(m)


# ─────────────────────────────────────────────────────────────────────────
# GET /migrations — list with filters
# ─────────────────────────────────────────────────────────────────────────


@router.get(
    "",
    response_model=list[MigrationOut],
    summary="List migrations (optional filter by app_id or target_topology_id)",
)
async def list_migrations(
    session: Annotated[AsyncSession, Depends(get_session)],
    app_id: Annotated[str | None, Query()] = None,
    target_topology_id: Annotated[int | None, Query(ge=1)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[MigrationOut]:
    rows = await migration_engine.list_migrations(
        session,
        target_topology_id=target_topology_id,
        app_id=app_id,
        limit=limit,
    )
    return [_to_migration_out(r) for r in rows]


# ─────────────────────────────────────────────────────────────────────────
# POST /migrations/{id}/rollback — manual rollback
# ─────────────────────────────────────────────────────────────────────────


@router.post(
    "/{migration_id}/rollback",
    response_model=MigrationOut,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Manually roll back a migration",
    description=(
        "Transitions the migration to ROLLING_BACK and kicks off the "
        "rollback engine in the background. Works on both COMPLETED "
        "migrations (the 'undo a successful migration' workflow) and "
        "in-progress migrations whose engine has stalled or which an "
        "operator wants to abort manually.\n\n"
        "Refuses if the migration is already in a non-COMPLETED "
        "terminal state (ROLLED_BACK or ROLLBACK_FAILED)."
    ),
)
async def manual_rollback(
    migration_id: int,
    body: RollbackRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> MigrationOut:
    try:
        await rollback_engine.start_manual_rollback(
            session,
            migration_id=migration_id,
            operator=body.operator,
            reason=body.reason,
            session_factory=get_session_factory(),
        )
    except LookupError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc),
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(exc),
        ) from exc

    m = await migration_engine.get_migration(session, migration_id)
    if m is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="migration row vanished",
        )
    return _to_migration_out(m)


# ─────────────────────────────────────────────────────────────────────────
# GET /migrations/{id}/audit — scoped audit excerpt
# ─────────────────────────────────────────────────────────────────────────


@router.get(
    "/{migration_id}/audit",
    summary="Audit-log entries scoped to one migration's correlation_id",
    description=(
        "Returns AuditLog rows whose correlation_id matches the running "
        "migration's correlation_id. The correlation_id is captured "
        "on the MIGRATION_PLANNED audit entry; this endpoint resolves "
        "it from there. Lamport-ordered ascending."
    ),
)
async def get_migration_audit(
    migration_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
) -> dict[str, Any]:
    # Resolve the migration's correlation_id by finding its
    # MIGRATION_PLANNED audit row (there's exactly one per migration).
    from bcl.models.orm import AuditOperation

    m = await session.get(Migration, migration_id)
    if m is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"migration {migration_id} not found",
        )

    stmt = (
        select(AuditLog)
        .where(
            AuditLog.operation == AuditOperation.MIGRATION_PLANNED,
        )
        .order_by(AuditLog.lamport_clock.desc())
    )
    rows = (await session.execute(stmt)).scalars().all()
    correlation_id: str | None = None
    for r in rows:
        rp = r.request_payload or {}
        if rp.get("migration_id") == migration_id:
            correlation_id = r.correlation_id
            break

    if correlation_id is None:
        return {
            "migration_id": migration_id,
            "correlation_id": None,
            "entries": [],
            "note": (
                "no MIGRATION_PLANNED audit entry found yet; the "
                "background task may not have written its first row."
            ),
        }

    # Fetch every audit entry sharing that correlation_id, ascending
    # Lamport order (causal order).
    #
    # Manual rollbacks (POST /migrations/{id}/rollback) write audit
    # entries under a separate correlation_id of the form
    # `rollback-{migration_id}-{epoch}`. Include those too so the UI's
    # Lamport timeline shows the full causal story (forward steps,
    # then ROLLBACK_INITIATED, ROLLBACK_STEP rows, ROLLBACK_COMPLETED).
    # Without this, a manually rolled-back migration shows state=
    # ROLLED_BACK with no rollback entries in the timeline.
    rollback_cid_prefix = f"rollback-{migration_id}-"
    entry_stmt = (
        select(AuditLog)
        .where(
            (AuditLog.correlation_id == correlation_id)
            | AuditLog.correlation_id.like(f"{rollback_cid_prefix}%")
        )
        .order_by(AuditLog.lamport_clock.asc())
        .limit(limit)
    )
    entries = (await session.execute(entry_stmt)).scalars().all()

    return {
        "migration_id": migration_id,
        "correlation_id": correlation_id,
        "count": len(entries),
        "entries": [
            {
                "id": e.id,
                "lamport_clock": e.lamport_clock,
                "wall_clock": e.wall_clock.isoformat(),
                "operation": e.operation.value,
                "actor": e.actor,
                "qm_name": e.qm_name,
                "success": e.success,
                "duration_ms": e.duration_ms,
                "is_rollback": e.is_rollback,
                "request_payload": e.request_payload,
                "response_payload": e.response_payload,
                "error_message": e.error_message,
            }
            for e in entries
        ],
    }


# ─────────────────────────────────────────────────────────────────────────
# GET /migrations/{id}/plan — the persisted planner output
# ─────────────────────────────────────────────────────────────────────────


@router.get(
    "/{migration_id}/plan",
    summary="The Migration Planner's output for this migration",
    description=(
        "Returns the plan JSON persisted on Migration.plan. Includes "
        "the planner's narrative, ordering rationale, predicted "
        "duration, queues to redirect, risks, and rollback strategy. "
        "Surfaces the audit metadata (which model produced it, "
        "duration, fallback or LLM)."
    ),
)
async def get_migration_plan(
    migration_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    m = await session.get(Migration, migration_id)
    if m is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"migration {migration_id} not found",
        )
    if m.plan is None:
        return {
            "migration_id": migration_id,
            "plan": None,
            "note": "plan not yet computed (state=PLANNED, agent pending)",
        }
    return {
        "migration_id": migration_id,
        "state": m.state.value,
        **m.plan,
    }


# ─────────────────────────────────────────────────────────────────────────
# GET /migrations/{id}/drain — drain-wait progress + Little's Law
# ─────────────────────────────────────────────────────────────────────────


@router.get(
    "/{migration_id}/drain",
    summary="Drain-wait progress (Little's Law prediction + poll history)",
    description=(
        "Returns the most recent drain-related ValidationRun for this "
        "migration. Includes per-queue poll history (used to draw the "
        "Little's Law widget): initial depth L_0, measured service "
        "rate μ, polls taken, time elapsed, error_kind if not yet "
        "drained.\n\n"
        "Reference: Little, J. D. C. (1961). 'A Proof for the Queuing "
        "Formula: L = λW'. Operations Research, 9(3), 383-387."
    ),
)
async def get_migration_drain(
    migration_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    from bcl.models.orm import ValidationRun, ValidationKind

    m = await session.get(Migration, migration_id)
    if m is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"migration {migration_id} not found",
        )

    # Most recent FUNCTIONAL validation run with drain evidence.
    stmt = (
        select(ValidationRun)
        .where(
            ValidationRun.migration_id == migration_id,
            ValidationRun.kind == ValidationKind.FUNCTIONAL,
        )
        .order_by(ValidationRun.started_at.desc())
        .limit(5)
    )
    rows = (await session.execute(stmt)).scalars().all()

    drain_history: list[dict[str, Any]] = []
    for r in rows:
        if isinstance(r.evidence, dict) and "drains" in r.evidence:
            drain_history.append({
                "started_at": r.started_at.isoformat(),
                "completed_at": r.completed_at.isoformat(),
                "outcome": r.outcome.value,
                "drains": r.evidence["drains"],
            })

    return {
        "migration_id": migration_id,
        "state": m.state.value,
        "drain_runs": drain_history,
        "note": (
            "Drain prediction follows Little's Law: T_drain ≈ L_0 / μ. "
            "μ is measured from the first ~1.5s of polling and "
            "surfaced in each drain entry's measured_mu field."
        ),
        "reference": (
            "Little, J. D. C. (1961). \"A Proof for the Queuing "
            "Formula: L = λW\". Operations Research, 9(3), 383-387."
        ),
    }


__all__ = ["router"]
