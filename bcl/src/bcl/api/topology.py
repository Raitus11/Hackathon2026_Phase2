"""Topology endpoints."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bcl.audit.writer import write_audit_entry
from bcl.db.session import get_session
from bcl.models.api import (
    ApplicationOut,
    QueueManagerOut,
    TopologyOut,
    TopologySpec,
)
from bcl.models.orm import (
    Application,
    AuditOperation,
    QueueManager,
    Topology,
)

router = APIRouter(prefix="/topologies", tags=["topology"])


@router.post("", response_model=TopologyOut, status_code=status.HTTP_201_CREATED,
             summary="Create a topology from a flow spec")
async def create_topology(
    spec: TopologySpec,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> TopologyOut:
    existing = await session.execute(
        select(Topology).where(Topology.name == spec.name)
    )
    if existing.scalar_one_or_none() is not None:
        await write_audit_entry(
            session,
            operation=AuditOperation.GUARDRAIL_REJECTED,
            success=False,
            request_payload={"name": spec.name, "kind": spec.kind.value},
            error_message=f"topology name '{spec.name}' already exists",
        )
        await session.commit()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Topology with name '{spec.name}' already exists",
        )

    now = datetime.now(UTC)

    topology = Topology(
        name=spec.name,
        kind=spec.kind,
        spec={"flows": [f.model_dump() for f in spec.flows]},
        created_at=now,
    )
    session.add(topology)
    await session.flush()

    seen_apps: dict[str, Application] = {}
    for flow in spec.flows:
        for app_id, app_name, neighbourhood in [
            (flow.producer_app_id, flow.producer_app_name, flow.producer_neighbourhood),
            (flow.consumer_app_id, flow.consumer_app_name, flow.consumer_neighnourhood),
        ]:
            if app_id not in seen_apps:
                existing_app = await session.get(Application, app_id)
                if existing_app is None:
                    app = Application(
                        app_id=app_id,
                        app_name=app_name,
                        neighbourhood=neighbourhood,
                        created_at=now,
                    )
                    session.add(app)
                    seen_apps[app_id] = app
                else:
                    seen_apps[app_id] = existing_app

    seen_qms: dict[str, QueueManager] = {}
    for flow in spec.flows:
        for qm_name in (flow.producer_queue_manager, flow.consumer_queue_manager):
            if qm_name not in seen_qms:
                qm = QueueManager(
                    topology_id=topology.id,
                    qm_name=qm_name,
                    pod_name=None,
                    service_name=None,
                    is_ready=False,
                )
                session.add(qm)
                seen_qms[qm_name] = qm

    await session.flush()

    await write_audit_entry(
        session,
        operation=AuditOperation.TOPOLOGY_CREATED,
        success=True,
        request_payload={
            "name": spec.name,
            "kind": spec.kind.value,
            "flow_count": len(spec.flows),
        },
        response_payload={
            "topology_id": topology.id,
            "app_count": len(seen_apps),
            "qm_count": len(seen_qms),
        },
        state_after={"topology_id": topology.id},
    )

    await session.commit()
    await session.refresh(topology, attribute_names=["queue_managers"])

    return TopologyOut(
        id=topology.id,
        name=topology.name,
        kind=topology.kind,
        spec=topology.spec,
        created_at=topology.created_at,
        queue_managers=[
            QueueManagerOut(
                id=qm.id, qm_name=qm.qm_name, pod_name=qm.pod_name,
                service_name=qm.service_name, listener_port=qm.listener_port,
                web_port=qm.web_port, dlq_name=qm.dlq_name,
                deployed_at=qm.deployed_at, is_ready=qm.is_ready,
            )
            for qm in topology.queue_managers
        ],
    )


@router.get("", response_model=list[TopologyOut], summary="List all topologies")
async def list_topologies(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> list[TopologyOut]:
    result = await session.execute(
        select(Topology).order_by(Topology.created_at.desc())
    )
    topologies = result.scalars().all()
    return [
        TopologyOut(
            id=t.id, name=t.name, kind=t.kind, spec=t.spec,
            created_at=t.created_at, queue_managers=[],
        )
        for t in topologies
    ]


@router.get("/{topology_id}", response_model=TopologyOut, summary="Get one topology with QM detail")
async def get_topology(
    topology_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> TopologyOut:
    topology = await session.get(Topology, topology_id)
    if topology is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Topology {topology_id} not found",
        )
    await session.refresh(topology, attribute_names=["queue_managers"])
    return TopologyOut(
        id=topology.id, name=topology.name, kind=topology.kind, spec=topology.spec,
        created_at=topology.created_at,
        queue_managers=[
            QueueManagerOut(
                id=qm.id, qm_name=qm.qm_name, pod_name=qm.pod_name,
                service_name=qm.service_name, listener_port=qm.listener_port,
                web_port=qm.web_port, dlq_name=qm.dlq_name,
                deployed_at=qm.deployed_at, is_ready=qm.is_ready,
            )
            for qm in topology.queue_managers
        ],
    )


@router.get("/{topology_id}/applications", response_model=list[ApplicationOut],
            summary="Distinct applications in this topology")
async def list_topology_applications(
    topology_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> list[ApplicationOut]:
    topology = await session.get(Topology, topology_id)
    if topology is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Topology {topology_id} not found",
        )
    flows = topology.spec.get("flows", [])
    app_ids: set[str] = set()
    for f in flows:
        app_ids.add(f["producer_app_id"])
        app_ids.add(f["consumer_app_id"])
    if not app_ids:
        return []
    result = await session.execute(
        select(Application).where(Application.app_id.in_(app_ids))
    )
    apps = result.scalars().all()
    return [
        ApplicationOut(
            app_id=a.app_id, app_name=a.app_name,
            neighbourhood=a.neighbourhood, created_at=a.created_at,
        )
        for a in apps
    ]
