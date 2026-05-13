"""Provisioning endpoints.

POST   /topologies/{topology_id}/provision                  start a run (async)
GET    /topologies/{topology_id}/provision/{run_id}/status  poll progress
GET    /topologies/{topology_id}/provision                  list runs
DELETE /topologies/{topology_id}/provision                  tear down all QMs

The POST returns 202 Accepted with `run_id` immediately. The actual work
runs in a background task. Clients poll the status endpoint until
`state in {COMPLETED, FAILED, PARTIALLY_COMPLETED}`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bcl.audit.writer import write_audit_entry
from bcl.db.session import get_session, get_session_factory
from bcl.models.orm import (
    AuditOperation,
    ProvisionRun,
    ProvisionState,
    QueueManager,
    Topology,
)
from bcl.provisioning import engine, naming
from bcl.provisioning.k8s_client import K8sClient

router = APIRouter(prefix="/topologies", tags=["provisioning"])


# ─────────────────────────────────────────────────────────────────────────
# Request / response schemas
# ─────────────────────────────────────────────────────────────────────────


class ProvisionRequest(BaseModel):
    """Body for POST /topologies/{id}/provision."""

    actor: str = Field(
        default="operator:anon",
        min_length=1,
        max_length=64,
        description="Identity of the operator initiating the run; audit-logged.",
    )
    message: str | None = Field(
        default=None,
        max_length=512,
        description="Optional free-text note attached to the run.",
    )
    dry_run: bool = Field(
        default=False,
        description=(
            "If true, render manifests and emit progress events but do NOT "
            "apply to the cluster. Useful for testing/preview."
        ),
    )


class ProvisionRunOut(BaseModel):
    """Returned from POST and GET endpoints."""

    run_id: str
    topology_id: int
    state: ProvisionState
    qms_total: int
    qms_ready: int
    qms_failed: int
    started_at: datetime
    finished_at: datetime | None
    correlation_id: str
    actor: str
    operator_message: str | None
    error: str | None
    progress: list[dict[str, Any]]

    @classmethod
    def from_orm_row(cls, row: ProvisionRun) -> "ProvisionRunOut":
        return cls(
            run_id=row.run_id,
            topology_id=row.topology_id,
            state=row.state,
            qms_total=row.qms_total,
            qms_ready=row.qms_ready,
            qms_failed=row.qms_failed,
            started_at=row.started_at,
            finished_at=row.finished_at,
            correlation_id=row.correlation_id,
            actor=row.actor,
            operator_message=row.operator_message,
            error=row.error,
            progress=row.progress or [],
        )


# ─────────────────────────────────────────────────────────────────────────
# Endpoints
# ─────────────────────────────────────────────────────────────────────────


@router.post(
    "/{topology_id}/provision",
    response_model=ProvisionRunOut,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Provision all queue managers in a topology",
    description=(
        "Asynchronously deploy K8s resources (PVC, Secret, Deployment, "
        "Service) for every queue manager in the topology. Returns "
        "immediately with a `run_id` you can poll via "
        "GET /topologies/{id}/provision/{run_id}/status.\n\n"
        "Each QM gets its own PVC on `sc-ontap-nas`, a Secret with "
        "randomly-generated admin/app passwords mounted as files, a "
        "Deployment based on the WF-canonical MQ pattern, and a "
        "ClusterIP Service exposing 1414 (listener) and 9443 (web)."
    ),
)
async def provision_topology(
    topology_id: int,
    body: ProvisionRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ProvisionRunOut:
    topology = await session.get(Topology, topology_id)
    if topology is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Topology {topology_id} not found",
        )

    try:
        run = await engine.start_provision_run(
            session,
            topology_id=topology_id,
            actor=body.actor,
            operator_message=body.message,
            session_factory=get_session_factory(),
            dry_run=body.dry_run,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc

    return ProvisionRunOut.from_orm_row(run)


@router.get(
    "/{topology_id}/provision/{run_id}/status",
    response_model=ProvisionRunOut,
    summary="Poll a provisioning run's progress",
    description=(
        "Returns the current state, counters, and per-QM progress events. "
        "Clients should poll until `state in {COMPLETED, FAILED, "
        "PARTIALLY_COMPLETED}`. Recommended poll interval: 3 seconds."
    ),
)
async def get_provision_run_status(
    topology_id: int,
    run_id: str,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ProvisionRunOut:
    run = await engine.get_run(session, run_id)
    if run is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Provision run '{run_id}' not found",
        )
    if run.topology_id != topology_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                f"Run '{run_id}' exists but belongs to topology "
                f"{run.topology_id}, not {topology_id}"
            ),
        )
    return ProvisionRunOut.from_orm_row(run)


@router.get(
    "/{topology_id}/provision",
    response_model=list[ProvisionRunOut],
    summary="List all provisioning runs for a topology",
)
async def list_provision_runs(
    topology_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
    limit: int = Query(default=20, ge=1, le=100),
) -> list[ProvisionRunOut]:
    topology = await session.get(Topology, topology_id)
    if topology is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Topology {topology_id} not found",
        )
    runs = await engine.list_runs_for_topology(session, topology_id)
    return [ProvisionRunOut.from_orm_row(r) for r in runs[:limit]]


@router.delete(
    "/{topology_id}/provision",
    status_code=status.HTTP_200_OK,
    summary="Tear down all K8s resources for this topology",
    description=(
        "Deletes the Service, Deployment, Secret, and PVC for every "
        "queue manager owned by this topology. With reclaimPolicy=Delete "
        "on `sc-ontap-nas`, the underlying NAS volumes are also "
        "removed. Idempotent — already-absent resources are skipped."
    ),
)
async def teardown_topology(
    topology_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[str, Query(min_length=1, max_length=64)] = "operator:anon",
) -> dict[str, Any]:
    from bcl.config import get_settings

    topology = await session.get(Topology, topology_id)
    if topology is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Topology {topology_id} not found",
        )

    qms_stmt = select(QueueManager).where(QueueManager.topology_id == topology_id)
    qms = (await session.execute(qms_stmt)).scalars().all()

    settings = get_settings()
    client = K8sClient(namespace=settings.namespace)

    deleted: list[dict[str, Any]] = []

    for qm in qms:
        app_id = qm.qm_name   # see engine.py rationale
        names = {
            "Service": naming.service_name(app_id),
            "Deployment": naming.deployment_name(app_id),
            "Secret": naming.secret_name(app_id),
            "PersistentVolumeClaim": naming.pvc_name(app_id),
        }
        # Delete in reverse-apply order: Service, Deployment, Secret, PVC
        for kind, name in names.items():
            result = await client.delete_resource(
                kind, name, ignore_not_found=True
            )
            deleted.append(
                {
                    "qm_name": qm.qm_name, "kind": kind, "name": name,
                    "ok": result.ok, "exit_code": result.exit_code,
                }
            )
            await write_audit_entry(
                session,
                operation=AuditOperation.QM_DELETED,
                success=result.ok,
                actor=actor,
                qm_name=qm.qm_name,
                request_payload={"kind": kind, "name": name},
                response_payload=result.as_audit_payload(),
                error_message=None if result.ok else result.stderr[:1000],
            )

        # Mark QM as not-ready in the DB; clear pod/service names
        qm.is_ready = False
        qm.pod_name = None
        # service_name stays — it's the DNS name, may be reprovisioned later

    await session.commit()

    return {
        "topology_id": topology_id,
        "deleted_count": len(deleted),
        "deleted": deleted,
    }


# ═════════════════════════════════════════════════════════════════════════
# MQ object realization endpoints
# ═════════════════════════════════════════════════════════════════════════
#
# These create / tear down the MQ-level objects (queues, channels, XMITQs)
# INSIDE the queue managers, after K8s provisioning has stood up the pods.
#
# Same async-run-with-polling pattern as the K8s provisioning above.
#
# Layering:
#   POST /topologies/{id}/provision         -> stand up pods (engine.py)
#   POST /topologies/{id}/realize-mq-objects -> populate MQ objects (mq_realize.py)
#   DELETE /topologies/{id}/realize-mq-objects -> tear down MQ objects only
#   DELETE /topologies/{id}/provision        -> tear down pods (existing above)
# ═════════════════════════════════════════════════════════════════════════


from bcl.models.orm import MqRealizeRun, MqRealizeState  # noqa: E402
from bcl.provisioning import mq_realize  # noqa: E402


class RealizeRequest(BaseModel):
    """Body for POST /topologies/{id}/realize-mq-objects."""

    actor: str = Field(
        default="operator:anon",
        min_length=1,
        max_length=64,
        description="Identity of the operator initiating the run; audit-logged.",
    )
    message: str | None = Field(
        default=None,
        max_length=512,
        description="Optional free-text note attached to the run.",
    )
    dry_run: bool = Field(
        default=False,
        description=(
            "If true, derive plans and emit progress events but do NOT "
            "send MQSC to the QM pods. Useful for previewing what would "
            "be applied."
        ),
    )


class RealizeRunOut(BaseModel):
    """Returned from POST/GET realize endpoints. Mirrors ProvisionRunOut."""

    run_id: str
    topology_id: int
    direction: Literal["APPLY", "TEARDOWN"]
    state: MqRealizeState
    qms_total: int
    qms_completed: int
    qms_failed: int
    commands_total: int
    commands_applied: int
    commands_skipped_idempotent: int
    commands_failed: int
    started_at: datetime
    finished_at: datetime | None
    correlation_id: str
    actor: str
    operator_message: str | None
    error: str | None
    progress: list[dict[str, Any]]
    derived_plans_summary: dict[str, Any] | None

    @classmethod
    def from_orm_row(cls, row: MqRealizeRun) -> "RealizeRunOut":
        return cls(
            run_id=row.run_id,
            topology_id=row.topology_id,
            direction=row.direction,  # type: ignore[arg-type]
            state=row.state,
            qms_total=row.qms_total,
            qms_completed=row.qms_completed,
            qms_failed=row.qms_failed,
            commands_total=row.commands_total,
            commands_applied=row.commands_applied,
            commands_skipped_idempotent=row.commands_skipped_idempotent,
            commands_failed=row.commands_failed,
            started_at=row.started_at,
            finished_at=row.finished_at,
            correlation_id=row.correlation_id,
            actor=row.actor,
            operator_message=row.operator_message,
            error=row.error,
            progress=row.progress or [],
            derived_plans_summary=row.derived_plans_summary,
        )


# We import Literal here rather than at module top to keep the original
# file's import block undisturbed (existing modules: typing.Annotated, Any).
from typing import Literal  # noqa: E402


@router.post(
    "/{topology_id}/realize-mq-objects",
    response_model=RealizeRunOut,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Create MQ objects (queues, channels, XMITQs) inside provisioned QMs",
    description=(
        "Derives an MQSC plan for every queue manager in the topology, then "
        "applies it via `runmqsc` inside each QM pod. Idempotent: every "
        "DEFINE uses REPLACE, and already-exists AMQ codes are tolerated. "
        "Returns 202 immediately with a `run_id`; poll "
        "GET /topologies/{id}/realize-mq-objects/{run_id}/status until "
        "`state in {COMPLETED, FAILED, PARTIALLY_COMPLETED}`.\n\n"
        "**Pre-requisite:** every QM in the topology must be provisioned "
        "(i.e. POST /topologies/{id}/provision must have completed for "
        "all QMs). If any QM is missing a pod, the request returns 400."
    ),
)
async def realize_mq_objects(
    topology_id: int,
    body: RealizeRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> RealizeRunOut:
    topology = await session.get(Topology, topology_id)
    if topology is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Topology {topology_id} not found",
        )

    try:
        run = await mq_realize.start_realize_run(
            session,
            topology_id=topology_id,
            direction="APPLY",
            actor=body.actor,
            operator_message=body.message,
            session_factory=get_session_factory(),
            dry_run=body.dry_run,
        )
    except LookupError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc

    return RealizeRunOut.from_orm_row(run)


@router.delete(
    "/{topology_id}/realize-mq-objects",
    response_model=RealizeRunOut,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Tear down MQ objects only (leaves pods running)",
    description=(
        "The inverse of POST: derives the forward MQSC plan, then computes "
        "its inverse (DELETE commands in reverse order with the DLQ and "
        "QMGR-level commands skipped), and applies. Idempotent: "
        "not-found AMQ codes are tolerated.\n\n"
        "Use this when you want to recreate MQ objects without re-deploying "
        "pods (e.g. after fixing the topology spec). To also tear down pods, "
        "follow this with DELETE /topologies/{id}/provision, or use the "
        "cascade-delete on the topology resource."
    ),
)
async def teardown_mq_objects(
    topology_id: int,
    body: RealizeRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> RealizeRunOut:
    topology = await session.get(Topology, topology_id)
    if topology is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Topology {topology_id} not found",
        )

    try:
        run = await mq_realize.start_realize_run(
            session,
            topology_id=topology_id,
            direction="TEARDOWN",
            actor=body.actor,
            operator_message=body.message,
            session_factory=get_session_factory(),
            dry_run=body.dry_run,
        )
    except LookupError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc

    return RealizeRunOut.from_orm_row(run)


@router.get(
    "/{topology_id}/realize-mq-objects/{run_id}/status",
    response_model=RealizeRunOut,
    summary="Poll an MQ-realize run's progress",
)
async def get_realize_run_status(
    topology_id: int,
    run_id: str,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> RealizeRunOut:
    run = await mq_realize.get_run(session, run_id)
    if run is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Realize run '{run_id}' not found",
        )
    if run.topology_id != topology_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                f"Run '{run_id}' exists but belongs to topology "
                f"{run.topology_id}, not {topology_id}"
            ),
        )
    return RealizeRunOut.from_orm_row(run)


@router.get(
    "/{topology_id}/realize-mq-objects",
    response_model=list[RealizeRunOut],
    summary="List all MQ-realize runs for a topology",
)
async def list_realize_runs(
    topology_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
    limit: int = Query(default=20, ge=1, le=100),
) -> list[RealizeRunOut]:
    topology = await session.get(Topology, topology_id)
    if topology is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Topology {topology_id} not found",
        )
    runs = await mq_realize.list_runs_for_topology(session, topology_id)
    return [RealizeRunOut.from_orm_row(r) for r in runs[:limit]]


__all__ = ["router"]
