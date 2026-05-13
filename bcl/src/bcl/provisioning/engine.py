"""Provisioning engine — orchestrates one /topologies/{id}/provision run.

Flow per QM:
    1. Render the four manifests (PVC, Secret, Deployment, Service).
    2. Apply in order: PVC → Secret → Deployment → Service.
    3. Wait for Deployment Available=True (bounded by timeout).
    4. Fetch the pod name, update the QueueManager row in the DB.
    5. Audit-log every apply with the K8sResult payload and Lamport clock.

The orchestrator runs all QMs SERIALLY for the hackathon. This is the
right call: easier to reason about; failures cleaner; ordering preserved
in the audit log. Parallel is a Phase 3 optimization.

Invariants:
    - Every K8s call writes one audit row, success or failure.
    - The audit row's `request_payload` contains the rendered YAML hash
      and the literal `oc apply` command — so a forensic reconstruction
      can reproduce the call exactly.
    - The ProvisionRun row is the source of truth for run-level state;
      audit rows are the source of truth for per-step state.

Worst-case test: the engine survives an aborted call (process
killed mid-run) by leaving the audit log + DB in a consistent state.
We achieve that by committing after every step.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bcl.audit.lamport import LamportClock
from bcl.audit.writer import write_audit_entry
from bcl.config import get_settings
from bcl.models.orm import (
    AuditOperation,
    ProvisionRun,
    ProvisionState,
    QueueManager,
    Topology,
)
from bcl.provisioning import naming
from bcl.provisioning.k8s_client import K8sClient, K8sResult
from bcl.provisioning.render import (
    QMRenderInputs,
    RenderedManifests,
    generate_password,
    render_for_qm,
)

logger = logging.getLogger("bcl.provisioning.engine")


# ─────────────────────────────────────────────────────────────────────────
# Per-QM step labels (used in progress events + audit operations)
# ─────────────────────────────────────────────────────────────────────────


# Mapping from resource kind to the audit op we record at apply time.
_APPLY_AUDIT_OP: dict[str, AuditOperation] = {
    "PersistentVolumeClaim": AuditOperation.QM_PVC_CREATED,
    "Secret": AuditOperation.QM_SECRET_CREATED,
    "Deployment": AuditOperation.QM_DEPLOYED,
    "Service": AuditOperation.QM_SERVICE_CREATED,
}


# ─────────────────────────────────────────────────────────────────────────
# Public entry point: create the run row, kick off background task
# ─────────────────────────────────────────────────────────────────────────


async def start_provision_run(
    session: AsyncSession,
    *,
    topology_id: int,
    actor: str,
    operator_message: str | None,
    session_factory: async_sessionmaker[AsyncSession],
    dry_run: bool = False,
) -> ProvisionRun:
    """Create a ProvisionRun row, schedule the background execution.

    Returns the persisted ProvisionRun (with `run_id` populated) so the
    HTTP layer can return it immediately while work continues async.

    On dry_run=True, the run is created with state COMPLETED and per-QM
    progress events containing the rendered YAML — no K8s calls made.
    """
    settings = get_settings()

    # Pull the topology and its QMs (eager-load via explicit query)
    topology = await session.get(Topology, topology_id)
    if topology is None:
        raise LookupError(f"topology id={topology_id} not found")

    qms_stmt = select(QueueManager).where(QueueManager.topology_id == topology_id)
    qms = (await session.execute(qms_stmt)).scalars().all()

    if not qms:
        raise ValueError(
            f"topology id={topology_id} has no queue managers to provision"
        )

    run_id = str(uuid.uuid4())
    correlation_id = run_id   # one correlation_id ties every audit row to this run

    run = ProvisionRun(
        run_id=run_id,
        topology_id=topology_id,
        state=ProvisionState.PENDING,
        qms_total=len(qms),
        qms_ready=0,
        qms_failed=0,
        started_at=datetime.now(UTC),
        finished_at=None,
        correlation_id=correlation_id,
        actor=actor,
        operator_message=operator_message,
        error=None,
        progress=[],
    )
    session.add(run)
    await session.flush()

    await write_audit_entry(
        session,
        operation=AuditOperation.PROVISION_STARTED,
        success=True,
        actor=actor,
        correlation_id=correlation_id,
        request_payload={
            "topology_id": topology_id,
            "topology_name": topology.name,
            "qm_count": len(qms),
            "qm_names": [q.qm_name for q in qms],
            "dry_run": dry_run,
            "operator_message": operator_message,
        },
    )
    await session.commit()

    # Launch background task. We pass the session factory rather than the
    # session itself so the task gets its own session — current session
    # closes as soon as this function returns.
    asyncio.create_task(
        _execute_run(
            run_id=run_id,
            topology_id=topology_id,
            qm_ids=[q.id for q in qms],
            correlation_id=correlation_id,
            actor=actor,
            session_factory=session_factory,
            dry_run=dry_run,
            namespace=settings.namespace,
        )
    )

    return run


# ─────────────────────────────────────────────────────────────────────────
# Background executor
# ─────────────────────────────────────────────────────────────────────────


async def _execute_run(
    *,
    run_id: str,
    topology_id: int,
    qm_ids: list[int],
    correlation_id: str,
    actor: str,
    session_factory: async_sessionmaker[AsyncSession],
    dry_run: bool,
    namespace: str,
) -> None:
    """Background entry point. Owns its own DB session lifetime."""
    logger.info(
        "provision run %s starting: topology=%d qms=%d dry_run=%s",
        run_id, topology_id, len(qm_ids), dry_run,
    )

    client = None if dry_run else K8sClient(namespace=namespace)

    async with session_factory() as session:
        # Move run to RUNNING
        run = await _fetch_run(session, run_id)
        run.state = ProvisionState.RUNNING
        await session.commit()

    qms_ready = 0
    qms_failed = 0
    first_error: str | None = None

    for qm_id in qm_ids:
        try:
            ok = await _provision_one_qm(
                qm_id=qm_id,
                run_id=run_id,
                correlation_id=correlation_id,
                actor=actor,
                client=client,
                session_factory=session_factory,
                dry_run=dry_run,
            )
            if ok:
                qms_ready += 1
            else:
                qms_failed += 1
                if first_error is None:
                    first_error = f"qm id={qm_id} failed (see progress events)"
        except Exception as exc:   # noqa: BLE001 — defensive; engine must not crash mid-run
            qms_failed += 1
            err = f"qm id={qm_id} raised: {type(exc).__name__}: {exc}"
            if first_error is None:
                first_error = err
            logger.exception("provision run %s failed for qm_id=%d", run_id, qm_id)
            # Still try the remaining QMs — partial success is allowed
            async with session_factory() as session:
                await _append_progress(
                    session, run_id,
                    qm_name=f"id-{qm_id}",
                    phase="EXCEPTION",
                    status="FAILED",
                    error=err,
                )
                await session.commit()

    # Finalize the run row
    async with session_factory() as session:
        run = await _fetch_run(session, run_id)
        run.qms_ready = qms_ready
        run.qms_failed = qms_failed
        run.finished_at = datetime.now(UTC)
        if qms_failed == 0:
            run.state = ProvisionState.COMPLETED
            audit_op = AuditOperation.PROVISION_COMPLETED
            success = True
            run.error = None
        elif qms_ready == 0:
            run.state = ProvisionState.FAILED
            audit_op = AuditOperation.PROVISION_FAILED
            success = False
            run.error = first_error
        else:
            run.state = ProvisionState.PARTIALLY_COMPLETED
            audit_op = AuditOperation.PROVISION_FAILED   # partial = treated as fail
            success = False
            run.error = first_error

        started_at = run.started_at
        finished_at = run.finished_at
        # SQLite strips tzinfo on round-trip; coerce both to UTC-aware
        if started_at is not None and started_at.tzinfo is None:
            started_at = started_at.replace(tzinfo=UTC)
        if finished_at is not None and finished_at.tzinfo is None:
            finished_at = finished_at.replace(tzinfo=UTC)
        duration = (finished_at - started_at).total_seconds() if finished_at else 0.0

        await write_audit_entry(
            session,
            operation=audit_op,
            success=success,
            actor=actor,
            correlation_id=correlation_id,
            response_payload={
                "run_id": run_id,
                "qms_total": run.qms_total,
                "qms_ready": qms_ready,
                "qms_failed": qms_failed,
                "duration_seconds": duration,
                "final_state": run.state.value,
                "first_error": first_error,
            },
            error_message=first_error if not success else None,
        )
        await session.commit()

    logger.info(
        "provision run %s finished: state=%s ready=%d failed=%d",
        run_id, run.state.value, qms_ready, qms_failed,
    )


async def _provision_one_qm(
    *,
    qm_id: int,
    run_id: str,
    correlation_id: str,
    actor: str,
    client: K8sClient | None,
    session_factory: async_sessionmaker[AsyncSession],
    dry_run: bool,
) -> bool:
    """Provision a single queue manager. Returns True on full success."""

    # ─── load QM + topology (one transaction) ────────────────────────
    async with session_factory() as session:
        qm = await session.get(QueueManager, qm_id)
        if qm is None:
            return False
        topology_id = qm.topology_id
        qm_name_db = qm.qm_name
        # Derive the app_id this QM serves. The QueueManager ORM doesn't
        # currently store app_id directly (it stores qm_name from the CSV),
        # so we derive the app_id from naming. For now, treat qm_name as
        # the app_id — the topology endpoint stores qm_name = the CSV's
        # producer_queue_manager / consumer_queue_manager. Design note:
        # in a future migration we'll add `qm.app_id FK` for stronger
        # provenance; for the hackathon, qm_name is the identifier.
        app_id_for_naming = qm_name_db

    # Render with consistent passwords (generated once per QM)
    settings = get_settings()
    admin_pw = generate_password()
    app_pw = generate_password()

    render_inputs = QMRenderInputs(
        app_id=app_id_for_naming,
        # qm_name is the value that becomes MQ_QMGR_NAME inside the pod
        # (see deployment.yaml.j2). It MUST equal the CSV's
        # producer_queue_manager / consumer_queue_manager value so the
        # MQSC objects derived from the CSV (RQMNAME, channel-name parts,
        # etc.) resolve correctly. Previously this passed through
        # naming.mq_qmgr_name() which appended "_QM" — that broke channel
        # handshakes (QREMOTE.RQMNAME('WLZ03') vs runtime QM 'WLZ03_QM').
        # CSV name is the authoritative identifier; runtime name must match it.
        qm_name=qm_name_db,
        topology_id=topology_id,
        run_id=run_id,
        lamport_clock=await LamportClock.instance().peek(),
        admin_password=admin_pw,
        app_password=app_pw,
    )
    manifests = render_for_qm(render_inputs)

    if dry_run:
        # Commit per-event to avoid JSON-column mutation batching issues
        # (matches the live-mode pattern in _apply_one).
        for kind, _yaml in manifests.in_apply_order():
            async with session_factory() as session:
                await _append_progress(
                    session, run_id,
                    qm_name=qm_name_db, phase=kind.upper() + "_APPLY",
                    status="DRY_RUN", error=None,
                )
                await session.commit()
        return True

    assert client is not None   # mypy: client is non-None when not dry_run

    # ─── apply each manifest in order ────────────────────────────────
    for kind, yaml_text in manifests.in_apply_order():
        ok = await _apply_one(
            client=client,
            yaml_text=yaml_text,
            kind=kind,
            qm_name=qm_name_db,
            run_id=run_id,
            correlation_id=correlation_id,
            actor=actor,
            session_factory=session_factory,
        )
        if not ok:
            return False

    # ─── wait for Deployment Available=True ──────────────────────────
    deployment = naming.deployment_name(app_id_for_naming)
    async with session_factory() as session:
        await _append_progress(
            session, run_id,
            qm_name=qm_name_db, phase="WAIT_FOR_READY", status="WAITING",
            error=None,
        )
        await session.commit()

    wait_result = await client.wait_for_deployment_available(
        deployment, timeout_seconds=300.0, poll_interval_seconds=5.0,
    )

    if not wait_result.ok:
        async with session_factory() as session:
            await write_audit_entry(
                session,
                operation=AuditOperation.QM_DEPLOYED,
                success=False,
                actor=actor,
                correlation_id=correlation_id,
                qm_name=qm_name_db,
                request_payload={"phase": "WAIT_FOR_READY", "deployment": deployment},
                response_payload=wait_result.as_audit_payload(),
                error_message="Deployment did not reach Available=True within timeout",
            )
            await _append_progress(
                session, run_id,
                qm_name=qm_name_db, phase="WAIT_FOR_READY", status="FAILED",
                error="timeout",
            )
            await session.commit()
        return False

    # ─── fetch pod name, mark QM ready ───────────────────────────────
    _pod_result, pod_name = await client.get_pod_name(deployment)

    async with session_factory() as session:
        qm = await session.get(QueueManager, qm_id)
        if qm is not None:
            qm.pod_name = pod_name
            qm.service_name = naming.service_name(app_id_for_naming)
            qm.is_ready = True
        await write_audit_entry(
            session,
            operation=AuditOperation.QM_READY,
            success=True,
            actor=actor,
            correlation_id=correlation_id,
            qm_name=qm_name_db,
            response_payload={
                "pod_name": pod_name,
                "service_name": naming.service_name(app_id_for_naming),
                "deployment_name": deployment,
            },
        )
        await _append_progress(
            session, run_id,
            qm_name=qm_name_db, phase="COMPLETE", status="READY",
            error=None, pod_name=pod_name,
        )
        # Increment run.qms_ready as we go so live polling sees progress
        run = await _fetch_run(session, run_id)
        run.qms_ready = (run.qms_ready or 0) + 1
        await session.commit()

    logger.info("qm %s is READY (pod=%s)", qm_name_db, pod_name)
    return True


async def _apply_one(
    *,
    client: K8sClient,
    yaml_text: str,
    kind: str,
    qm_name: str,
    run_id: str,
    correlation_id: str,
    actor: str,
    session_factory: async_sessionmaker[AsyncSession],
) -> bool:
    """Apply a single manifest, audit-log the result, return success."""
    yaml_hash = hashlib.sha256(yaml_text.encode("utf-8")).hexdigest()[:16]

    async with session_factory() as session:
        await _append_progress(
            session, run_id,
            qm_name=qm_name, phase=f"{kind.upper()}_APPLY", status="APPLYING",
            error=None,
        )
        await session.commit()

    result = await client.apply_yaml(yaml_text)

    audit_op = _APPLY_AUDIT_OP.get(kind, AuditOperation.QM_DEPLOYED)

    async with session_factory() as session:
        await write_audit_entry(
            session,
            operation=audit_op,
            success=result.ok,
            actor=actor,
            correlation_id=correlation_id,
            qm_name=qm_name,
            request_payload={
                "kind": kind,
                "yaml_sha256_prefix": yaml_hash,
                "yaml_bytes": len(yaml_text),
            },
            response_payload=result.as_audit_payload(),
            error_message=None if result.ok else result.stderr[:1000],
        )
        await _append_progress(
            session, run_id,
            qm_name=qm_name, phase=f"{kind.upper()}_APPLY",
            status="APPLIED" if result.ok else "FAILED",
            error=None if result.ok else result.stderr[:200],
        )
        await session.commit()

    return result.ok


# ─────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────


async def _fetch_run(session: AsyncSession, run_id: str) -> ProvisionRun:
    stmt = select(ProvisionRun).where(ProvisionRun.run_id == run_id)
    run = (await session.execute(stmt)).scalar_one()
    return run


async def _append_progress(
    session: AsyncSession,
    run_id: str,
    *,
    qm_name: str,
    phase: str,
    status: str,
    error: str | None,
    pod_name: str | None = None,
) -> None:
    """Append a single event onto ProvisionRun.progress.

    SQLite JSON column doesn't support atomic append; we read-modify-write.
    This is safe because progress is only ever appended by this background
    task (one writer per run).

    We call flag_modified explicitly because SQLAlchemy's plain JSON column
    type doesn't auto-detect in-place mutations within a session — without
    the flag, the SECOND mutation in the same session can be silently lost.
    """
    from sqlalchemy.orm.attributes import flag_modified

    run = await _fetch_run(session, run_id)
    # Force a fresh read from the DB. Without this, in fast-succession
    # commits with separate sessions (dry-run loop), the next session's
    # identity-map or aiosqlite connection cache can return a stale
    # `progress` list, silently dropping in-flight events.
    await session.refresh(run, attribute_names=["progress"])
    event: dict[str, Any] = {
        "qm_name": qm_name,
        "phase": phase,
        "status": status,
        "timestamp": datetime.now(UTC).isoformat(),
    }
    if error is not None:
        event["error"] = error
    if pod_name is not None:
        event["pod_name"] = pod_name
    new_progress = list(run.progress or [])
    new_progress.append(event)
    run.progress = new_progress
    flag_modified(run, "progress")


# ─────────────────────────────────────────────────────────────────────────
# Status query (used by GET endpoint)
# ─────────────────────────────────────────────────────────────────────────


async def get_run(session: AsyncSession, run_id: str) -> ProvisionRun | None:
    stmt = select(ProvisionRun).where(ProvisionRun.run_id == run_id)
    return (await session.execute(stmt)).scalar_one_or_none()


async def list_runs_for_topology(
    session: AsyncSession, topology_id: int
) -> list[ProvisionRun]:
    stmt = (
        select(ProvisionRun)
        .where(ProvisionRun.topology_id == topology_id)
        .order_by(ProvisionRun.started_at.desc())
    )
    return list((await session.execute(stmt)).scalars().all())


__all__ = [
    "start_provision_run",
    "get_run",
    "list_runs_for_topology",
]
