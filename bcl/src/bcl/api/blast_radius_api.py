"""Migration blast-radius endpoints — per-app co-tenancy + isolation proof.

GET /topologies/{source_id}/blast-radius?app=<app_id>&target_id=<id>
        Full blast-radius analysis for migrating one app: co-tenants,
        per-shared-QM exposure, and the isolation proof (the enumerated
        migration MQSC, with the count of commands touching a co-tenant's
        queue — 0 when per-queue isolation holds).

GET /topologies/{source_id}/migration-order
        Advisory greedy co-tenancy-degree ordering across all apps.

Both endpoints are strictly read-only. They parse stored topology specs
and reuse the migration engine's own ownership + rewire-plan logic to
ENUMERATE (never execute) MQSC. No MQ calls, no database writes.

Rationale — why this exists
---------------------------
Mainframe-fronted source queue managers are shared by several apps. The
recurring judge question is: how is one app migrated off a shared QM
without disturbing the queues of the apps still on it? This endpoint
answers it as a measurement — see bcl.analysis.blast_radius.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bcl.analysis.blast_radius import (
    analyse_blast_radius,
    recommend_migration_order,
)
from bcl.config import get_settings
from bcl.db.session import get_session
from bcl.models.orm import Topology, TopologyKind

router = APIRouter(prefix="/topologies", tags=["blast-radius"])


# ─────────────────────────────────────────────────────────────────────────
# Response schemas
# ─────────────────────────────────────────────────────────────────────────


class BlastRadiusOut(BaseModel):
    """GET /topologies/{id}/blast-radius response."""

    app_id: str
    source_qm: str | None
    target_qm: str | None

    is_mainframe_fronted: bool
    neighbourhoods: list[str]

    shared_qm_exposure: list[dict[str, Any]]
    """Per source QM the app touches: qm, is_shared, migrating_app_queues,
    cotenant_apps, cotenant_queue_count."""

    cotenants: list[dict[str, Any]]
    """Per co-tenant app: app_id, shared_qm, queues_on_shared_qm."""

    isolation: dict[str, Any]
    """The proof: total_migration_commands,
    commands_touching_migrating_app, commands_touching_cotenant_exclusive
    (the disturbance count — 0 means isolated),
    cotenant_exclusive_queues_in_blast_radius,
    cotenants_with_rerouted_traffic (affected-not-disturbed apps),
    disturbed."""

    summary: str
    references: list[str]


class MigrationOrderOut(BaseModel):
    """GET /topologies/{id}/migration-order response."""

    recommended_order: list[str]
    per_app_cotenancy_degree: dict[str, int]
    rationale: str
    reference: str


# ─────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────


async def _get_topology(
    session: AsyncSession, topology_id: int
) -> Topology:
    topology = await session.get(Topology, topology_id)
    if topology is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Topology {topology_id} not found",
        )
    return topology


async def _resolve_target(
    session: AsyncSession,
    source: Topology,
    target_id: int | None,
) -> Topology:
    """Resolve the TARGET topology to migrate towards.

    If target_id is given, use it. Otherwise pick the most recent
    TARGET-kind topology — there is normally exactly one in a demo run.
    """
    if target_id is not None:
        target = await _get_topology(session, target_id)
        if target.kind != TopologyKind.TARGET:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"Topology {target_id} is {target.kind.value}, "
                    "expected a TARGET topology"
                ),
            )
        return target

    rows = await session.execute(
        select(Topology)
        .where(Topology.kind == TopologyKind.TARGET)
        .order_by(Topology.created_at.desc())
    )
    target = rows.scalars().first()
    if target is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                "No TARGET topology found. Upload the target topology "
                "first, or pass ?target_id=<id> explicitly."
            ),
        )
    return target


# ─────────────────────────────────────────────────────────────────────────
# Endpoints
# ─────────────────────────────────────────────────────────────────────────


@router.get(
    "/{source_id}/blast-radius",
    response_model=BlastRadiusOut,
    summary="Blast-radius analysis for migrating one app off a shared QM",
    description=(
        "Computes which other apps share a source queue manager with the "
        "named app (its co-tenants), which queues the migration will "
        "rewire, and the isolation proof: the migration's REWIRING MQSC "
        "is enumerated (never executed) and every command is checked "
        "against the co-tenants' queues. `commands_touching_cotenants` "
        "is the count of migration commands that target a co-tenant's "
        "queue — 0 when the migration is per-queue isolated.\n\n"
        "Read-only. Issues no MQSC and writes nothing. The ownership and "
        "rewire-plan logic is the migration engine's own, so this "
        "analysis cannot drift from what a real migration would do."
    ),
)
async def get_blast_radius(
    source_id: int,
    app: Annotated[str, Query(description="App id to analyse, e.g. LIY/KW")],
    session: Annotated[AsyncSession, Depends(get_session)],
    target_id: Annotated[
        int | None,
        Query(description="Target topology id (defaults to latest TARGET)"),
    ] = None,
) -> BlastRadiusOut:
    source = await _get_topology(session, source_id)
    if source.kind != TopologyKind.SOURCE:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Topology {source_id} is {source.kind.value}, expected "
                "a SOURCE topology"
            ),
        )
    target = await _resolve_target(session, source, target_id)

    settings = get_settings()
    result = analyse_blast_radius(
        app_id=app,
        source_topology_spec=source.spec,
        target_topology_spec=target.spec,
        target_qm_namespace=settings.namespace,
    )

    if result.source_qm is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                f"App '{app}' was not found in source topology "
                f"{source_id}."
            ),
        )

    return BlastRadiusOut(
        app_id=result.app_id,
        source_qm=result.source_qm,
        target_qm=result.target_qm,
        is_mainframe_fronted=result.is_mainframe_fronted,
        neighbourhoods=result.neighbourhoods,
        shared_qm_exposure=[
            {
                "qm": e.qm,
                "is_shared": e.is_shared,
                "migrating_app_queues": e.migrating_app_queues,
                "cotenant_apps": e.cotenant_apps,
                "cotenant_queue_count": e.cotenant_queue_count,
            }
            for e in result.shared_qm_exposure
        ],
        cotenants=[
            {
                "app_id": c.app_id,
                "shared_qm": c.shared_qm,
                "queues_on_shared_qm": c.queues_on_shared_qm,
            }
            for c in result.cotenants
        ],
        isolation={
            "total_migration_commands": result.isolation.total_migration_commands,
            "commands_touching_migrating_app": (
                result.isolation.commands_touching_migrating_app
            ),
            "commands_touching_cotenant_exclusive": (
                result.isolation.commands_touching_cotenant_exclusive
            ),
            "cotenant_exclusive_queues_in_blast_radius": (
                result.isolation.cotenant_exclusive_queues_in_blast_radius
            ),
            "cotenants_with_rerouted_traffic": (
                result.isolation.cotenants_with_rerouted_traffic
            ),
            "disturbed": result.isolation.disturbed,
        },
        summary=result.summary,
        references=result.references,
    )


@router.get(
    "/{source_id}/migration-order",
    response_model=MigrationOrderOut,
    summary="Advisory co-tenancy-degree migration order for all apps",
    description=(
        "Greedy ascending-co-tenancy-degree ordering of every app in the "
        "source topology: apps sharing queue managers with fewer other "
        "apps are recommended first, minimising the time a shared QM "
        "spends partially migrated.\n\n"
        "Advisory only — the migration engine does not consume this; the "
        "operator chooses order. Greedy heuristic, not an exact optimum "
        "(deliberate for a small app count)."
    ),
)
async def get_migration_order(
    source_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> MigrationOrderOut:
    source = await _get_topology(session, source_id)
    if source.kind != TopologyKind.SOURCE:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Topology {source_id} is {source.kind.value}, expected "
                "a SOURCE topology"
            ),
        )

    result = recommend_migration_order(source_topology_spec=source.spec)
    return MigrationOrderOut(
        recommended_order=result.recommended_order,
        per_app_cotenancy_degree=result.per_app_cotenancy_degree,
        rationale=result.rationale,
        reference=result.reference,
    )
