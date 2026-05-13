"""MQ-object realization orchestrator.

Layer above the K8s provisioning engine. Once `engine.py` has stood up
the pods and the QMs inside them, this engine creates the MQ objects
(queues, channels, XMITQs) those QMs need according to a topology's
flow spec.

Flow per QM (APPLY direction):
    1. Derive the MQSC plan via mqsc_derivation.derive_mqsc_for_qm().
    2. Resolve the QM's pod name from the DB (QueueManager.pod_name,
       populated by engine.py at deploy time).
    3. For each MQSC command in plan.commands:
         a. Apply via MqClient.apply_mqsc(qm, pod, mqsc_text).
         b. Audit-log with the matching MQSC_* op + correlation_id.
         c. Tolerate already-exists AMQ codes as success
            (AMQ8350 queue exists, AMQ8013 obj exists, AMQ8348 already).
    4. Roll counters on the MqRealizeRun row, append progress event.

Flow per QM (TEARDOWN direction):
    1. Derive the forward plan as above.
    2. Compute inverse_plan(fwd) — DELETE commands in reverse order.
    3. Apply each inverse command. Tolerate not-found AMQ codes as
       success (AMQ8147 queue doesn't exist, AMQ8016 channel not found).
    4. Audit-log with is_rollback=True so the audit log filter shows
       these as teardown events distinct from forward operations.

The orchestrator runs QMs SERIALLY (same call as engine.py). Per-command
within a QM is also serial — we want deterministic ordering for the audit
log and a clean per-step audit trail.

Crash safety: commits after every command. A process kill mid-run leaves
the audit log + DB in a consistent state. Resume is manual: the operator
re-issues POST or DELETE; idempotency at the MQSC level (REPLACE on
DEFINE; AMQ-tolerance on DELETE) makes re-application safe.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bcl.audit.lamport import LamportClock
from bcl.audit.writer import write_audit_entry
from bcl.config import get_settings
from bcl.models.api import FlowSpec
from bcl.models.orm import (
    AuditOperation,
    MqRealizeRun,
    MqRealizeState,
    QueueManager,
    Topology,
)
from bcl.provisioning.mq_client import MqClient, MqscResult
from bcl.provisioning.mqsc_derivation import (
    MqscCommand,
    MqscPlan,
    derive_mqsc_for_qm,
    inverse_plan,
)

logger = logging.getLogger("bcl.provisioning.mq_realize")


# ─────────────────────────────────────────────────────────────────────────
# Idempotency: AMQ codes treated as success
# ─────────────────────────────────────────────────────────────────────────

# Per MQ docs https://www.ibm.com/docs/en/ibm-mq/9.4?topic=messages-amq8xxx
# the following codes mean "object already in target state" and are safe
# to treat as successful idempotent outcomes.
#
# APPLY direction — "already exists" is success:
#   AMQ8350E - MQSC name conflict (used by DEFINE when object exists w/o REPLACE)
#   AMQ8013E - existing object cannot be replaced (sometimes seen on channels)
#   AMQ8348E - command syntactically valid but object already defined
#
# TEARDOWN direction — "doesn't exist" is success:
#   AMQ8147E - MQ object 'X' not found
#   AMQ8016E - channel 'X' not found
#   AMQ8260E - queue does not exist (some MQ versions)
#
# Note: with REPLACE on every DEFINE, AMQ8350/AMQ8013/AMQ8348 should be
# rare during APPLY — but we tolerate them anyway as defence in depth.

_APPLY_IDEMPOTENT_AMQ: frozenset[str] = frozenset({
    "AMQ8350E", "AMQ8013E", "AMQ8348E",
})

_TEARDOWN_IDEMPOTENT_AMQ: frozenset[str] = frozenset({
    "AMQ8147E", "AMQ8016E", "AMQ8260E",
})


def _is_idempotent_success(
    amq_code: str | None, direction: Literal["APPLY", "TEARDOWN"]
) -> bool:
    """Decide whether an AMQ error code should be counted as success.

    Returns True when the failure is semantically a no-op for our direction
    (object already exists / already absent).
    """
    if amq_code is None:
        return False
    if direction == "APPLY":
        return amq_code in _APPLY_IDEMPOTENT_AMQ
    return amq_code in _TEARDOWN_IDEMPOTENT_AMQ


# ─────────────────────────────────────────────────────────────────────────
# Public entry point
# ─────────────────────────────────────────────────────────────────────────


async def start_realize_run(
    session: AsyncSession,
    *,
    topology_id: int,
    direction: Literal["APPLY", "TEARDOWN"],
    actor: str,
    operator_message: str | None,
    session_factory: async_sessionmaker[AsyncSession],
    dry_run: bool = False,
) -> MqRealizeRun:
    """Create an MqRealizeRun row and schedule the background execution.

    Returns the run row immediately so the HTTP handler can return 202
    with the run_id; the actual MQSC work happens in the background.

    The function:
      1. Validates the topology exists and has QMs.
      2. Validates every QM has a pod_name (otherwise it hasn't been
         provisioned yet — caller is asking for MQ-object realize on
         pods that don't exist).
      3. Pre-derives every QM's plan to populate qms_total /
         commands_total and capture a snapshot in derived_plans_summary.
      4. Writes an audit entry (operation: MQSC_ALTER_QMGR with is_rollback
         flag based on direction).
      5. Launches the background task.
    """
    settings = get_settings()

    topology = await session.get(Topology, topology_id)
    if topology is None:
        raise LookupError(f"topology id={topology_id} not found")

    qms_stmt = select(QueueManager).where(QueueManager.topology_id == topology_id)
    qms = list((await session.execute(qms_stmt)).scalars().all())
    if not qms:
        raise ValueError(
            f"topology id={topology_id} has no queue managers to realize"
        )

    # For APPLY: require every QM has been provisioned (has pod_name).
    # For TEARDOWN: tolerate missing pods (they may have been torn down
    # at the K8s layer separately; the run will skip those QMs).
    not_ready = [q.qm_name for q in qms if not q.pod_name or not q.is_ready]
    if direction == "APPLY" and not_ready:
        raise ValueError(
            f"cannot realize MQ objects: {len(not_ready)} QM(s) not "
            f"provisioned yet: {not_ready[:5]}"
            f"{' (and more)' if len(not_ready) > 5 else ''}. "
            "Run POST /topologies/{id}/provision first."
        )

    # Pre-derive plans to compute totals + snapshot.
    flows = [FlowSpec.model_validate(f) for f in topology.spec.get("flows", [])]
    plans: dict[str, MqscPlan] = {}
    for qm in qms:
        plan = derive_mqsc_for_qm(
            qm_name=qm.qm_name,
            flows=flows,
            namespace=settings.namespace,
            listener_port=settings.mq_listener_port,
            dlq_name=qm.dlq_name,
        )
        if direction == "TEARDOWN":
            plan = inverse_plan(plan)
        plans[qm.qm_name] = plan

    commands_total = sum(len(p.commands) for p in plans.values())
    derived_summary = {qm: plan.to_summary_dict() for qm, plan in plans.items()}

    run_id = str(uuid.uuid4())
    correlation_id = run_id

    run = MqRealizeRun(
        run_id=run_id,
        topology_id=topology_id,
        direction=direction,
        state=MqRealizeState.PENDING,
        qms_total=len(qms),
        qms_completed=0,
        qms_failed=0,
        commands_total=commands_total,
        commands_applied=0,
        commands_skipped_idempotent=0,
        commands_failed=0,
        started_at=datetime.now(UTC),
        finished_at=None,
        correlation_id=correlation_id,
        actor=actor,
        operator_message=operator_message,
        error=None,
        progress=[],
        derived_plans_summary=derived_summary,
    )
    session.add(run)
    await session.flush()

    await write_audit_entry(
        session,
        operation=AuditOperation.MQSC_ALTER_QMGR,  # umbrella op for run-start
        success=True,
        actor=actor,
        correlation_id=correlation_id,
        request_payload={
            "kind": "mq_realize_run_started",
            "topology_id": topology_id,
            "direction": direction,
            "qm_count": len(qms),
            "commands_total": commands_total,
            "dry_run": dry_run,
        },
        is_rollback=(direction == "TEARDOWN"),
    )
    await session.commit()

    asyncio.create_task(
        _execute_realize_run(
            run_id=run_id,
            topology_id=topology_id,
            direction=direction,
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


async def _execute_realize_run(
    *,
    run_id: str,
    topology_id: int,
    direction: Literal["APPLY", "TEARDOWN"],
    correlation_id: str,
    actor: str,
    session_factory: async_sessionmaker[AsyncSession],
    dry_run: bool,
    namespace: str,
) -> None:
    """Background entry point. Owns its own session lifetime."""
    logger.info(
        "realize run %s starting: topology=%d direction=%s dry_run=%s",
        run_id, topology_id, direction, dry_run,
    )

    client = MqClient(default_namespace=namespace)

    # Move state to RUNNING.
    async with session_factory() as session:
        run = await _fetch_run(session, run_id)
        run.state = MqRealizeState.RUNNING
        await session.commit()

    # Load topology + QMs in their own session (closed before we start
    # the long per-QM loop, so background workers don't hold DB locks).
    async with session_factory() as session:
        topology = await session.get(Topology, topology_id)
        if topology is None:
            await _finalize_run(
                session_factory, run_id, qms_completed=0, qms_failed=0,
                commands_applied=0, commands_skipped=0, commands_failed=0,
                first_error=f"topology id={topology_id} disappeared mid-run",
            )
            return
        flows = [FlowSpec.model_validate(f) for f in topology.spec.get("flows", [])]
        qms_stmt = select(QueueManager).where(
            QueueManager.topology_id == topology_id
        )
        qms = list((await session.execute(qms_stmt)).scalars().all())
        # Snapshot what we need; close the session.
        qm_records = [
            (q.id, q.qm_name, q.pod_name, q.is_ready, q.dlq_name)
            for q in qms
        ]
        settings = get_settings()
        listener_port = settings.mq_listener_port

    qms_completed = 0
    qms_failed = 0
    commands_applied = 0
    commands_skipped = 0
    commands_failed = 0
    first_error: str | None = None

    for qm_id, qm_name, pod_name, is_ready, dlq_name in qm_records:
        try:
            outcome = await _realize_one_qm(
                qm_id=qm_id,
                qm_name=qm_name,
                pod_name=pod_name,
                is_ready=is_ready,
                dlq_name=dlq_name,
                flows=flows,
                direction=direction,
                run_id=run_id,
                correlation_id=correlation_id,
                actor=actor,
                client=client,
                session_factory=session_factory,
                dry_run=dry_run,
                namespace=namespace,
                listener_port=listener_port,
            )
            commands_applied += outcome["applied"]
            commands_skipped += outcome["skipped"]
            commands_failed += outcome["failed"]
            if outcome["failed"] == 0:
                qms_completed += 1
            else:
                qms_failed += 1
                if first_error is None and outcome.get("error"):
                    first_error = f"qm {qm_name}: {outcome['error']}"
        except Exception as exc:  # noqa: BLE001 — engine must not crash mid-run
            qms_failed += 1
            err = f"qm {qm_name} raised: {type(exc).__name__}: {exc}"
            if first_error is None:
                first_error = err
            logger.exception("realize run %s failed on qm %s", run_id, qm_name)
            async with session_factory() as session:
                await _append_progress(
                    session, run_id, qm_name=qm_name,
                    phase="EXCEPTION", status="FAILED", error=err,
                )
                await session.commit()

    await _finalize_run(
        session_factory, run_id,
        qms_completed=qms_completed, qms_failed=qms_failed,
        commands_applied=commands_applied,
        commands_skipped=commands_skipped,
        commands_failed=commands_failed,
        first_error=first_error,
    )


# ─────────────────────────────────────────────────────────────────────────
# Per-QM execution
# ─────────────────────────────────────────────────────────────────────────


async def _realize_one_qm(
    *,
    qm_id: int,
    qm_name: str,
    pod_name: str | None,
    is_ready: bool,
    dlq_name: str,
    flows: list[FlowSpec],
    direction: Literal["APPLY", "TEARDOWN"],
    run_id: str,
    correlation_id: str,
    actor: str,
    client: MqClient,
    session_factory: async_sessionmaker[AsyncSession],
    dry_run: bool,
    namespace: str,
    listener_port: int,
) -> dict[str, Any]:
    """Realize MQ objects on one QM.

    Returns a dict {applied, skipped, failed, error?} summarizing the run
    so the caller can roll counters.
    """
    # Derive the plan for this QM.
    plan = derive_mqsc_for_qm(
        qm_name=qm_name,
        flows=flows,
        namespace=namespace,
        listener_port=listener_port,
        dlq_name=dlq_name,
    )
    if direction == "TEARDOWN":
        plan = inverse_plan(plan)

    async with session_factory() as session:
        await _append_progress(
            session, run_id, qm_name=qm_name,
            phase="PLAN_DERIVED", status="OK",
            error=None,
            command_count=len(plan.commands),
            warnings=list(plan.warnings) if plan.warnings else None,
        )
        await session.commit()

    # Pod check. APPLY direction requires a pod (start_realize_run already
    # validated this, but be defensive — pod could have been torn down in
    # the gap between queue-up and execution). TEARDOWN tolerates a missing
    # pod by reporting "skipped: no pod"; the K8s teardown will handle the
    # actual object cleanup.
    if pod_name is None or not is_ready:
        async with session_factory() as session:
            await _append_progress(
                session, run_id, qm_name=qm_name,
                phase="POD_CHECK", status="SKIPPED",
                error=f"QM {qm_name} has no ready pod; skipping ({direction})",
            )
            await session.commit()
        if direction == "APPLY":
            return {
                "applied": 0, "skipped": 0, "failed": 1,
                "error": "no pod available for APPLY",
            }
        # TEARDOWN: treat the QM as already-torn-down. Count its commands
        # as skipped (idempotent no-op).
        return {
            "applied": 0, "skipped": len(plan.commands), "failed": 0,
        }

    if dry_run:
        # Dry-run: log the plan but don't execute. Use one progress event
        # per command for parity with the live path's audit-stream feel.
        for cmd in plan.commands:
            async with session_factory() as session:
                await _append_progress(
                    session, run_id, qm_name=qm_name,
                    phase=cmd.op_kind.value, status="DRY_RUN",
                    error=None,
                    mqsc=cmd.mqsc_text,
                )
                await session.commit()
        return {
            "applied": 0, "skipped": len(plan.commands), "failed": 0,
        }

    # Live execution. One subprocess per command for clean audit ordering
    # and per-command failure isolation. Sub-second latency per call on
    # an in-cluster `oc exec`, so the total cost for a 30-command QM is
    # ~5-10s — well within demo budget.
    applied = 0
    skipped = 0
    failed = 0
    first_qm_error: str | None = None

    for cmd in plan.commands:
        outcome = await _apply_one_command(
            cmd=cmd,
            qm_name=qm_name,
            pod_name=pod_name,
            direction=direction,
            client=client,
            run_id=run_id,
            correlation_id=correlation_id,
            actor=actor,
            session_factory=session_factory,
            namespace=namespace,
        )
        if outcome == "APPLIED":
            applied += 1
        elif outcome == "SKIPPED_IDEMPOTENT":
            skipped += 1
        else:  # FAILED
            failed += 1
            if first_qm_error is None:
                first_qm_error = (
                    f"command {cmd.op_kind.value} on {cmd.object_name} failed"
                )
            # Continue with remaining commands — partial success is allowed.
            # The MqRealizeRun reports failed count + first_error; operators
            # can inspect the audit log for the rest.

    async with session_factory() as session:
        await _append_progress(
            session, run_id, qm_name=qm_name,
            phase="COMPLETE",
            status="COMPLETED" if failed == 0 else "PARTIAL",
            error=first_qm_error,
        )
        # Bump run's per-QM counter so live polling sees progress.
        run = await _fetch_run(session, run_id)
        if failed == 0:
            run.qms_completed = (run.qms_completed or 0) + 1
        else:
            run.qms_failed = (run.qms_failed or 0) + 1
        run.commands_applied = (run.commands_applied or 0) + applied
        run.commands_skipped_idempotent = (
            (run.commands_skipped_idempotent or 0) + skipped
        )
        run.commands_failed = (run.commands_failed or 0) + failed
        await session.commit()

    return {
        "applied": applied, "skipped": skipped, "failed": failed,
        "error": first_qm_error,
    }


# ─────────────────────────────────────────────────────────────────────────
# Per-command execution
# ─────────────────────────────────────────────────────────────────────────


async def _apply_one_command(
    *,
    cmd: MqscCommand,
    qm_name: str,
    pod_name: str,
    direction: Literal["APPLY", "TEARDOWN"],
    client: MqClient,
    run_id: str,
    correlation_id: str,
    actor: str,
    session_factory: async_sessionmaker[AsyncSession],
    namespace: str,
) -> Literal["APPLIED", "SKIPPED_IDEMPOTENT", "FAILED"]:
    """Execute one MQSC command. Audit-log the outcome. Return classification."""

    result: MqscResult = await client.apply_mqsc(
        qm_name=qm_name,
        pod_name=pod_name,
        mqsc_text=cmd.mqsc_text,
        namespace=namespace,
    )

    # Classify the outcome. runmqsc semantics:
    #   result.all_succeeded: process exited 0, every command got an "I"
    #     severity AMQ code → APPLIED.
    #   result.has_failures + first command's AMQ in idempotent set →
    #     SKIPPED_IDEMPOTENT.
    #   otherwise → FAILED.
    first_amq = result.per_command[0].amq_code if result.per_command else None

    if result.all_succeeded:
        classification: Literal["APPLIED", "SKIPPED_IDEMPOTENT", "FAILED"] = "APPLIED"
        success = True
    elif _is_idempotent_success(first_amq, direction):
        classification = "SKIPPED_IDEMPOTENT"
        success = True
    else:
        classification = "FAILED"
        success = False

    await _write_command_audit(
        cmd=cmd,
        result=result,
        classification=classification,
        success=success,
        qm_name=qm_name,
        direction=direction,
        correlation_id=correlation_id,
        actor=actor,
        session_factory=session_factory,
    )

    async with session_factory() as session:
        await _append_progress(
            session, run_id, qm_name=qm_name,
            phase=cmd.op_kind.value,
            status=classification,
            error=(
                None if success
                else (result.raw_stderr[:200] or result.raw_stdout[:200])
            ),
            mqsc=cmd.mqsc_text,
            amq_code=first_amq,
        )
        await session.commit()

    return classification


async def _write_command_audit(
    *,
    cmd: MqscCommand,
    result: MqscResult,
    classification: str,
    success: bool,
    qm_name: str,
    direction: Literal["APPLY", "TEARDOWN"],
    correlation_id: str,
    actor: str,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Write one AuditLog entry summarizing one MQSC command."""
    async with session_factory() as session:
        await write_audit_entry(
            session,
            operation=cmd.op_kind,
            success=success,
            actor=actor,
            correlation_id=correlation_id,
            qm_name=qm_name,
            request_payload={
                "mqsc_text": cmd.mqsc_text,
                "object_kind": cmd.object_kind,
                "object_name": cmd.object_name,
                "rationale": cmd.rationale,
                "direction": direction,
                "related_flows": list(cmd.related_flows),
            },
            response_payload={
                "classification": classification,
                "exit_code": result.exit_code,
                "duration_seconds": round(result.duration_seconds, 3),
                "commands_processed": result.commands_processed,
                "syntax_errors": result.syntax_errors,
                "per_command": [
                    {
                        "amq_code": c.amq_code,
                        "severity": c.severity,
                        "detail": c.detail,
                    }
                    for c in result.per_command
                ],
                # Trim outputs to keep audit row reasonable.
                "stdout_tail": result.raw_stdout[-2000:],
                "stderr_tail": result.raw_stderr[-2000:],
            },
            error_message=(
                None if success
                else result.summary()
            ),
            duration_ms=int(result.duration_seconds * 1000),
            is_rollback=(direction == "TEARDOWN"),
        )
        await session.commit()


# ─────────────────────────────────────────────────────────────────────────
# Finalize + helpers
# ─────────────────────────────────────────────────────────────────────────


async def _finalize_run(
    session_factory: async_sessionmaker[AsyncSession],
    run_id: str,
    *,
    qms_completed: int,
    qms_failed: int,
    commands_applied: int,
    commands_skipped: int,
    commands_failed: int,
    first_error: str | None,
) -> None:
    """Set MqRealizeRun terminal state + write the run-completed audit row."""
    async with session_factory() as session:
        run = await _fetch_run(session, run_id)
        run.finished_at = datetime.now(UTC)
        run.qms_completed = qms_completed
        run.qms_failed = qms_failed
        run.commands_applied = commands_applied
        run.commands_skipped_idempotent = commands_skipped
        run.commands_failed = commands_failed

        if qms_failed == 0 and commands_failed == 0:
            run.state = MqRealizeState.COMPLETED
            success = True
        elif qms_completed == 0:
            run.state = MqRealizeState.FAILED
            success = False
        else:
            run.state = MqRealizeState.PARTIALLY_COMPLETED
            success = False
        run.error = first_error if not success else None

        started_at = run.started_at
        finished_at = run.finished_at
        if started_at is not None and started_at.tzinfo is None:
            started_at = started_at.replace(tzinfo=UTC)
        if finished_at is not None and finished_at.tzinfo is None:
            finished_at = finished_at.replace(tzinfo=UTC)
        duration = (
            (finished_at - started_at).total_seconds() if finished_at else 0.0
        )

        await write_audit_entry(
            session,
            operation=AuditOperation.MQSC_ALTER_QMGR,  # run-end umbrella op
            success=success,
            actor=run.actor,
            correlation_id=run.correlation_id,
            response_payload={
                "kind": "mq_realize_run_completed",
                "run_id": run_id,
                "direction": run.direction,
                "final_state": run.state.value,
                "qms_total": run.qms_total,
                "qms_completed": qms_completed,
                "qms_failed": qms_failed,
                "commands_total": run.commands_total,
                "commands_applied": commands_applied,
                "commands_skipped_idempotent": commands_skipped,
                "commands_failed": commands_failed,
                "duration_seconds": duration,
                "first_error": first_error,
            },
            error_message=first_error if not success else None,
            is_rollback=(run.direction == "TEARDOWN"),
        )
        await session.commit()

    logger.info(
        "realize run %s finished: state=%s qms %d/%d cmds applied=%d skipped=%d failed=%d",
        run_id, run.state.value, qms_completed, run.qms_total,
        commands_applied, commands_skipped, commands_failed,
    )


async def _fetch_run(session: AsyncSession, run_id: str) -> MqRealizeRun:
    stmt = select(MqRealizeRun).where(MqRealizeRun.run_id == run_id)
    return (await session.execute(stmt)).scalar_one()


async def _append_progress(
    session: AsyncSession,
    run_id: str,
    *,
    qm_name: str,
    phase: str,
    status: str,
    error: str | None,
    mqsc: str | None = None,
    amq_code: str | None = None,
    command_count: int | None = None,
    warnings: list[str] | None = None,
) -> None:
    """Append an event onto MqRealizeRun.progress. Same pattern as engine.py."""
    from sqlalchemy.orm.attributes import flag_modified

    run = await _fetch_run(session, run_id)
    await session.refresh(run, attribute_names=["progress"])
    event: dict[str, Any] = {
        "qm_name": qm_name,
        "phase": phase,
        "status": status,
        "timestamp": datetime.now(UTC).isoformat(),
    }
    if error is not None:
        event["error"] = error[:500]  # truncate
    if mqsc is not None:
        event["mqsc"] = mqsc
    if amq_code is not None:
        event["amq_code"] = amq_code
    if command_count is not None:
        event["command_count"] = command_count
    if warnings:
        event["warnings"] = warnings
    new_progress = list(run.progress or [])
    new_progress.append(event)
    run.progress = new_progress
    flag_modified(run, "progress")


# ─────────────────────────────────────────────────────────────────────────
# Status queries
# ─────────────────────────────────────────────────────────────────────────


async def get_run(session: AsyncSession, run_id: str) -> MqRealizeRun | None:
    stmt = select(MqRealizeRun).where(MqRealizeRun.run_id == run_id)
    return (await session.execute(stmt)).scalar_one_or_none()


async def list_runs_for_topology(
    session: AsyncSession, topology_id: int
) -> list[MqRealizeRun]:
    stmt = (
        select(MqRealizeRun)
        .where(MqRealizeRun.topology_id == topology_id)
        .order_by(MqRealizeRun.started_at.desc())
    )
    return list((await session.execute(stmt)).scalars().all())


__all__ = [
    "start_realize_run",
    "get_run",
    "list_runs_for_topology",
]
