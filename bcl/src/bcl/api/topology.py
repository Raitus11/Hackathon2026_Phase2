"""Topology endpoints."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
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
            (flow.consumer_app_id, flow.consumer_app_name, flow.consumer_neighbourhood),
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

    # QM count per topology in one grouped query — cheap, avoids
    # eager-loading every QueueManager row just to show a count.
    count_rows = await session.execute(
        select(QueueManager.topology_id, func.count())
        .group_by(QueueManager.topology_id)
    )
    qm_counts: dict[int, int] = {tid: n for tid, n in count_rows}

    return [
        TopologyOut(
            id=t.id, name=t.name, kind=t.kind, spec=t.spec,
            created_at=t.created_at, queue_managers=[],
            qm_count=qm_counts.get(t.id, 0),
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
        qm_count=len(topology.queue_managers),
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


# ═════════════════════════════════════════════════════════════════════════
# CSV ingest — POST /topologies/ingest-csv
# ═════════════════════════════════════════════════════════════════════════

# Imports for the CSV ingest path. Kept near the route to make this section
# self-contained and easy to lift into a sub-module later if it grows.
import csv as _csv_module        # noqa: E402
import io as _io_module          # noqa: E402

from fastapi import File, Form, UploadFile  # noqa: E402

from bcl.models.api import FlowSpec, TopologySpec as _TopologySpec  # noqa: E402
from bcl.models.orm import TopologyKind as _TopologyKind  # noqa: E402


# Required CSV columns. We tolerate the historical `consumer_neighnourhood`
# typo on the wire — FlowSpec's AliasChoices accepts either spelling.
_CSV_REQUIRED_COLUMNS = {
    "flow_type",
    "producer_app_id",
    "producer_app_name",
    "producer_neighbourhood",
    "producer_queue_manager",
    "producer_queue_name",
    "producer_queue_type",
    "transmit_queue_name",
    "channel_name",
    "consumer_app_id",
    "consumer_app_name",
    # consumer_neighbourhood OR consumer_neighnourhood — checked separately
    "consumer_queue_manager",
    "consumer_queue_name",
    "consumer_queue_type",
}


def _validate_csv_columns(headers: list[str]) -> list[str]:
    """Validate header set; return list of error messages (empty = OK)."""
    errors: list[str] = []
    header_set = set(h.strip() for h in headers)
    missing = _CSV_REQUIRED_COLUMNS - header_set
    if missing:
        errors.append(
            f"missing required column(s): {sorted(missing)}"
        )
    if not (
        "consumer_neighbourhood" in header_set
        or "consumer_neighnourhood" in header_set
    ):
        errors.append(
            "missing column: either 'consumer_neighbourhood' (correct) "
            "or 'consumer_neighnourhood' (legacy typo) must be present"
        )
    return errors


@router.post(
    "/ingest-csv",
    response_model=TopologyOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create a topology by uploading a flow CSV",
    description=(
        "Accepts a multipart/form-data upload of the source or target "
        "topology CSV. The CSV must have the columns described in "
        "bcl/models/api.py FlowSpec. Either `consumer_neighbourhood` "
        "(correct) or `consumer_neighnourhood` (legacy typo) is accepted.\n\n"
        "On success, returns the persisted topology with its queue "
        "managers populated (1 QueueManager row per distinct "
        "producer_queue_manager / consumer_queue_manager value in the CSV).\n\n"
        "On row-level errors, returns 400 with a list of "
        "`{row, field, error}` entries pointing at the offending CSV row."
    ),
)
async def ingest_topology_csv(
    session: Annotated[AsyncSession, Depends(get_session)],
    name: Annotated[str, Form(min_length=1, max_length=64)],
    kind: Annotated[_TopologyKind, Form()],
    file: Annotated[UploadFile, File(description="CSV file (UTF-8 or UTF-8 BOM)")],
) -> TopologyOut:
    raw_bytes = await file.read()
    if not raw_bytes:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="uploaded CSV is empty",
        )

    # Decode — tolerate UTF-8 BOM (Excel exports often add one).
    try:
        text = raw_bytes.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"CSV decode failed (expected UTF-8): {exc}",
        ) from exc

    reader = _csv_module.DictReader(_io_module.StringIO(text))
    if reader.fieldnames is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="CSV has no header row",
        )

    header_errors = _validate_csv_columns(list(reader.fieldnames))
    if header_errors:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "header_errors": header_errors,
                "received_headers": list(reader.fieldnames),
            },
        )

    flows: list[FlowSpec] = []
    row_errors: list[dict[str, Any]] = []

    for row_index, raw_row in enumerate(reader, start=2):  # row 1 = header
        # Empty strings -> None for the optional fields (transmit_queue_name,
        # channel_name) so the FlowSpec validator's Remote-vs-Local rules
        # apply correctly.
        cleaned = {
            k: (v.strip() if v is not None else "") for k, v in raw_row.items()
        }
        if not cleaned.get("transmit_queue_name"):
            cleaned["transmit_queue_name"] = None  # type: ignore[assignment]
        if not cleaned.get("channel_name"):
            cleaned["channel_name"] = None  # type: ignore[assignment]
        try:
            flows.append(FlowSpec.model_validate(cleaned))
        except Exception as exc:  # pydantic ValidationError or value-level errors
            row_errors.append({
                "row": row_index,
                "error": str(exc).splitlines()[0][:500],
            })

    if row_errors:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "rows_with_errors": row_errors[:20],
                "total_row_errors": len(row_errors),
            },
        )

    # Build a TopologySpec and re-use the existing create_topology
    # validation + persistence path. This guarantees CSV-ingested and
    # JSON-POSTed topologies converge on the same DB state.
    try:
        spec = _TopologySpec(name=name, kind=kind, flows=flows)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"topology-level validation failed: {exc}",
        ) from exc

    # Defer to the canonical creation path. Re-use is intentional: any
    # rule that applies to JSON POST applies to CSV POST too.
    return await create_topology(spec, session)


# ═════════════════════════════════════════════════════════════════════════
# Cascade delete — DELETE /topologies/{id}?cascade=true
# ═════════════════════════════════════════════════════════════════════════
#
# Full lifecycle teardown: MQ objects -> K8s pods -> DB rows.
# Used for clean dev cycles ("blow it all away, start over") and as the
# safety hatch when a partial provision left orphaned resources.
# ═════════════════════════════════════════════════════════════════════════


from bcl.models.orm import (  # noqa: E402
    AuditOperation as _AuditOp,
    QueueManager as _QueueManager,
)


@router.delete(
    "/{topology_id}",
    status_code=status.HTTP_200_OK,
    summary="Delete a topology — with optional cascade through MQ + K8s + DB",
    description=(
        "By default (`cascade=false`), refuses to delete a topology that "
        "has any queue managers in `is_ready=true` state — you'd orphan "
        "running pods.\n\n"
        "With `cascade=true`:\n"
        "  1. Triggers MQ-object teardown (TEARDOWN realize run, ASYNC).\n"
        "  2. **Note**: K8s pod teardown and DB row deletion only happen "
        "via separate calls (DELETE /provision then this endpoint with "
        "cascade=false). This avoids a long-running synchronous handler. "
        "For a one-shot blow-away, prefer the Makefile target `make "
        "teardown-source` (or `teardown-target`) which sequences the calls.\n\n"
        "On success, returns the IDs of any background runs kicked off."
    ),
)
async def delete_topology(
    topology_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
    cascade: Annotated[bool, Query()] = False,
    actor: Annotated[str, Query(min_length=1, max_length=64)] = "operator:anon",
) -> dict[str, Any]:
    topology = await session.get(Topology, topology_id)
    if topology is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Topology {topology_id} not found",
        )

    # Inspect the QMs to decide what's safe.
    qms_stmt = select(_QueueManager).where(
        _QueueManager.topology_id == topology_id
    )
    qms = list((await session.execute(qms_stmt)).scalars().all())
    ready_qms = [q for q in qms if q.is_ready]

    if ready_qms and not cascade:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "topology has provisioned QMs — refusing to delete",
                "ready_qms": [q.qm_name for q in ready_qms],
                "hint": (
                    "Call DELETE /topologies/{id}/realize-mq-objects then "
                    "DELETE /topologies/{id}/provision before deleting the "
                    "topology row. Or set cascade=true to start the MQ-object "
                    "teardown asynchronously (you must still call /provision "
                    "DELETE afterwards)."
                ),
            },
        )

    realize_run_id: str | None = None

    if cascade and ready_qms:
        # Kick off MQ-object teardown. The actual DB row deletion happens
        # only on a subsequent call with cascade=false once the runs finish.
        # This mirrors how production teardowns work: explicit per-stage.
        from bcl.provisioning import mq_realize as _mq_realize  # noqa: E402

        try:
            run = await _mq_realize.start_realize_run(
                session,
                topology_id=topology_id,
                direction="TEARDOWN",
                actor=actor,
                operator_message="cascade-delete kicked off by DELETE /topologies/{id}",
                session_factory=get_session_factory(),
                dry_run=False,
            )
            realize_run_id = run.run_id
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"failed to kick off MQ teardown: {exc}",
            ) from exc

        await write_audit_entry(
            session,
            operation=_AuditOp.TOPOLOGY_DELETED,
            success=True,
            actor=actor,
            request_payload={
                "topology_id": topology_id,
                "cascade": True,
                "kicked_off_realize_run_id": realize_run_id,
                "ready_qms_at_request": [q.qm_name for q in ready_qms],
                "note": (
                    "Topology row NOT yet deleted. MQ teardown running async; "
                    "call DELETE /provision then this endpoint with cascade=false "
                    "to complete."
                ),
            },
        )
        await session.commit()

        return {
            "topology_id": topology_id,
            "deleted": False,
            "cascade_kicked_off": True,
            "realize_teardown_run_id": realize_run_id,
            "next_steps": [
                f"Poll GET /topologies/{topology_id}/realize-mq-objects/"
                f"{realize_run_id}/status until COMPLETED.",
                f"DELETE /topologies/{topology_id}/provision",
                f"DELETE /topologies/{topology_id}  (cascade=false; now safe)",
            ],
        }

    # No ready QMs OR cascade=false on an empty topology — safe to drop
    # the DB rows. ON DELETE CASCADE on the FK columns takes care of
    # queue_managers, provision_runs, mq_realize_runs.
    await write_audit_entry(
        session,
        operation=_AuditOp.TOPOLOGY_DELETED,
        success=True,
        actor=actor,
        request_payload={
            "topology_id": topology_id,
            "topology_name": topology.name,
            "qm_count": len(qms),
            "cascade": cascade,
        },
    )
    await session.delete(topology)
    await session.commit()

    return {
        "topology_id": topology_id,
        "deleted": True,
        "cascade_kicked_off": False,
        "realize_teardown_run_id": None,
    }
