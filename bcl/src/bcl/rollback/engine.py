"""Rollback engine — reverse-Lamport walker.

Walks the MigrationStep rows for one migration in REVERSE step_index
order. For each step that completed successfully (succeeded == True)
and has a non-null rollback_payload, executes the inverse MQSC against
the same QM/pod the forward command ran on.

Property satisfied (per-app rollback locality, $S_4$ in the TLA+ spec
sense): a rollback for migration M touches only MQSC objects that M's
forward path created or modified. Other migrations' state is invisible
to this engine.

Idempotency: same idempotency rules as the realize engine's TEARDOWN
direction — AMQ8147 (object not found), AMQ8016 (channel not found),
AMQ9531 (channel not in inactive state for STOP) all count as success.

Ordering: reverse step_index. This is equivalent to reverse Lamport
order because step_index is monotonically incremented in the same loop
that emits the forward Lamport audit entries (the engine commits step
N's audit before incrementing to N+1).

Crash safety: each inverse command commits before the next runs. A
process kill mid-rollback leaves a partially-undone state; the engine
treats this as "incomplete rollback" — on restart, an operator can
re-trigger rollback and the idempotent inverse commands will skip
already-undone steps.

Reference: Lamport, L. (1978). "Time, Clocks, and the Ordering of
Events in a Distributed System". CACM 21(7), 558-565.
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

from bcl.audit.writer import write_audit_entry
from bcl.models.orm import (
    AuditOperation,
    Migration,
    MigrationState,
    MigrationStep,
)
from bcl.provisioning.mq_client import MqClient

logger = logging.getLogger("bcl.rollback.engine")


# Idempotent AMQ codes for inverse operations (matches mq_realize's
# TEARDOWN posture).
_INVERSE_IDEMPOTENT_AMQ: frozenset[str] = frozenset({
    "AMQ8147E",  # object not found
    "AMQ8016E",  # channel not found
    "AMQ8260E",  # queue does not exist
    "AMQ9531E",  # channel not in inactive state (for STOP)
})


async def execute_rollback(
    *,
    migration_id: int,
    correlation_id: str,
    actor: str,
    session_factory: async_sessionmaker[AsyncSession],
    client: MqClient,
    source_qm: str,
    source_pod: str,
    target_qm: str,
    target_pod: str,
    namespace: str,
    trigger_reason: str,
) -> bool:
    """Execute a per-app rollback. Returns True on full success.

    Sets Migration.state to ROLLED_BACK on success, ROLLBACK_FAILED
    otherwise (terminal — requires human intervention).
    """
    logger.info(
        "rollback %s starting. reason: %s", migration_id, trigger_reason,
    )

    # Audit: rollback initiated
    async with session_factory() as session:
        await write_audit_entry(
            session,
            operation=AuditOperation.ROLLBACK_INITIATED,
            success=True,
            actor=actor,
            correlation_id=correlation_id,
            request_payload={
                "migration_id": migration_id,
                "trigger_reason": trigger_reason,
            },
            is_rollback=True,
        )
        await session.commit()

    # Fetch all completed forward steps in REVERSE step_index order.
    async with session_factory() as session:
        stmt = (
            select(MigrationStep)
            .where(
                MigrationStep.migration_id == migration_id,
                MigrationStep.succeeded.is_(True),
            )
            .order_by(MigrationStep.step_index.desc())
        )
        steps = list((await session.execute(stmt)).scalars().all())
        # Snapshot the data we need; we don't want to hold the session
        # across the rollback loop.
        step_records = [
            (
                s.id, s.step_index,
                dict(s.rollback_payload) if s.rollback_payload else None,
                s.payload.get("step_label", "") if s.payload else "",
                s.payload.get("object_kind", "") if s.payload else "",
                s.payload.get("object_name", "") if s.payload else "",
            )
            for s in steps
        ]

    total = len(step_records)
    succeeded_count = 0
    failed_count = 0
    failures: list[str] = []

    for step_id, step_index, rollback_payload, step_label, obj_kind, obj_name in step_records:
        if rollback_payload is None:
            # Step is not individually rollable (e.g. ALTER QMGR with
            # no captured prior state). Record + skip.
            async with session_factory() as session:
                await write_audit_entry(
                    session,
                    operation=AuditOperation.ROLLBACK_STEP,
                    success=True,
                    actor=actor,
                    correlation_id=correlation_id,
                    request_payload={
                        "migration_id": migration_id,
                        "step_id": step_id,
                        "step_index": step_index,
                        "step_label": step_label,
                        "object": f"{obj_kind}({obj_name})",
                        "outcome": "SKIPPED_NO_INVERSE",
                    },
                    is_rollback=True,
                )
                await session.commit()
            succeeded_count += 1
            continue

        target_pod_for = rollback_payload.get("target_qm_pod_for", "source")
        pod = source_pod if target_pod_for == "source" else target_pod
        qm = source_qm if target_pod_for == "source" else target_qm
        mqsc_text = rollback_payload.get("mqsc_text", "")

        if not mqsc_text:
            failures.append(
                f"step {step_index}: rollback_payload has no mqsc_text"
            )
            failed_count += 1
            continue

        t0 = time.monotonic()
        result = await client.apply_mqsc(
            qm_name=qm, pod_name=pod,
            mqsc_text=mqsc_text, namespace=namespace,
        )
        duration_ms = int((time.monotonic() - t0) * 1000)

        success = result.all_succeeded or _is_idempotent_inverse_ok(result)

        async with session_factory() as session:
            await write_audit_entry(
                session,
                operation=AuditOperation.ROLLBACK_STEP,
                success=success,
                actor=actor,
                correlation_id=correlation_id,
                qm_name=qm,
                request_payload={
                    "migration_id": migration_id,
                    "step_id": step_id,
                    "step_index": step_index,
                    "step_label": step_label,
                    "mqsc_text": mqsc_text,
                    "inverse_of_object": f"{obj_kind}({obj_name})",
                },
                response_payload={
                    "exit_code": result.exit_code,
                    "stdout_tail": result.raw_stdout[-1000:],
                    "stderr_tail": result.raw_stderr[-1000:],
                    "commands_processed": result.commands_processed,
                    "per_command": [
                        {
                            "amq_code": c.amq_code,
                            "severity": c.severity,
                            "detail": c.detail,
                        }
                        for c in result.per_command
                    ],
                },
                error_message=(
                    None if success else f"inverse mqsc failed on {qm}"
                ),
                duration_ms=duration_ms,
                is_rollback=True,
            )
            await session.commit()

        if success:
            succeeded_count += 1
        else:
            failed_count += 1
            failures.append(
                f"step {step_index} ({obj_kind}({obj_name})): "
                f"exit={result.exit_code}"
            )

    # Final state: ROLLED_BACK if every step's inverse succeeded.
    final_state = (
        MigrationState.ROLLED_BACK if failed_count == 0
        else MigrationState.ROLLBACK_FAILED
    )

    async with session_factory() as session:
        m = await session.get(Migration, migration_id)
        if m is not None:
            prev = m.state
            m.state = final_state
            m.version = m.version + 1
            m.completed_at = datetime.now(UTC)
            await write_audit_entry(
                session,
                operation=AuditOperation.MIGRATION_STATE_TRANSITION,
                success=True,
                actor=actor,
                correlation_id=correlation_id,
                app_id=m.app_id,
                request_payload={
                    "migration_id": migration_id,
                    "from_state": prev.value,
                    "to_state": final_state.value,
                },
                state_before={"state": prev.value},
                state_after={"state": final_state.value},
                is_rollback=True,
            )

        await write_audit_entry(
            session,
            operation=(
                AuditOperation.ROLLBACK_COMPLETED if failed_count == 0
                else AuditOperation.ROLLBACK_FAILED
            ),
            success=(failed_count == 0),
            actor=actor,
            correlation_id=correlation_id,
            request_payload={
                "migration_id": migration_id,
                "steps_total": total,
                "steps_succeeded": succeeded_count,
                "steps_failed": failed_count,
            },
            response_payload={"failures": failures[:20]} if failures else None,
            error_message=(
                None if failed_count == 0
                else f"{failed_count} of {total} inverse step(s) failed"
            ),
            is_rollback=True,
        )
        await session.commit()

    logger.info(
        "rollback %s finished: state=%s steps %d/%d (failed=%d)",
        migration_id, final_state.value, succeeded_count, total, failed_count,
    )
    return failed_count == 0


def _is_idempotent_inverse_ok(result: Any) -> bool:
    """Treat 'object already absent' AMQ codes as inverse-success."""
    if not result.per_command:
        return False
    first = result.per_command[0].amq_code
    return first in _INVERSE_IDEMPOTENT_AMQ


# ─────────────────────────────────────────────────────────────────────────
# Manual rollback entry — called by REST handler for ad-hoc rollbacks
# ─────────────────────────────────────────────────────────────────────────


async def start_manual_rollback(
    session: AsyncSession,
    *,
    migration_id: int,
    operator: str,
    reason: str,
    session_factory: async_sessionmaker[AsyncSession],
) -> Migration:
    """Operator-initiated rollback (via POST /migrations/{id}/rollback).

    Validates state, transitions to ROLLING_BACK, kicks off the
    execution as a background task. Returns the Migration row
    immediately for the HTTP 202 response.
    """
    import asyncio
    from bcl.config import get_settings

    m = await session.get(Migration, migration_id)
    if m is None:
        raise LookupError(f"migration id={migration_id} not found")

    from bcl.migration.states import is_terminal
    if is_terminal(m.state) and m.state != MigrationState.COMPLETED:
        raise ValueError(
            f"migration is in terminal non-COMPLETED state "
            f"{m.state.value}; cannot roll back further"
        )

    # If currently COMPLETED, we still allow rollback — that's the
    # 'undo a successful migration' operator workflow.
    correlation_id = f"rollback-{migration_id}-{int(datetime.now(UTC).timestamp())}"

    await write_audit_entry(
        session,
        operation=AuditOperation.ROLLBACK_INITIATED,
        success=True,
        actor=f"operator:{operator}",
        correlation_id=correlation_id,
        app_id=m.app_id,
        request_payload={
            "migration_id": migration_id,
            "trigger": "manual",
            "reason": reason,
        },
        is_rollback=True,
    )

    # Force-transition to ROLLING_BACK.
    prev_state = m.state
    m.state = MigrationState.ROLLING_BACK
    m.version = m.version + 1
    await write_audit_entry(
        session,
        operation=AuditOperation.MIGRATION_STATE_TRANSITION,
        success=True,
        actor=f"operator:{operator}",
        correlation_id=correlation_id,
        app_id=m.app_id,
        request_payload={
            "migration_id": migration_id,
            "from_state": prev_state.value,
            "to_state": MigrationState.ROLLING_BACK.value,
            "trigger": "manual",
        },
        state_before={"state": prev_state.value},
        state_after={"state": MigrationState.ROLLING_BACK.value},
        is_rollback=True,
    )
    await session.commit()

    settings = get_settings()

    # Resolve pods for the source + target QMs. We need to repeat the
    # lookup since the rollback may be triggered against a COMPLETED
    # migration where the engine state is no longer in memory.
    src_qm, tgt_qm, src_pod, tgt_pod = await _resolve_qms_pods(
        session, migration_id
    )

    if not (src_qm and tgt_qm and src_pod and tgt_pod):
        m.state = MigrationState.ROLLBACK_FAILED
        await write_audit_entry(
            session,
            operation=AuditOperation.ROLLBACK_FAILED,
            success=False,
            actor=f"operator:{operator}",
            correlation_id=correlation_id,
            app_id=m.app_id,
            error_message=(
                f"could not resolve source/target QM or pod for "
                f"rollback. src_qm={src_qm} tgt_qm={tgt_qm} "
                f"src_pod={src_pod} tgt_pod={tgt_pod}"
            ),
            is_rollback=True,
        )
        await session.commit()
        return m

    client = MqClient(default_namespace=settings.namespace)
    asyncio.create_task(
        execute_rollback(
            migration_id=migration_id,
            correlation_id=correlation_id,
            actor=f"operator:{operator}",
            session_factory=session_factory,
            client=client,
            source_qm=src_qm, source_pod=src_pod,
            target_qm=tgt_qm, target_pod=tgt_pod,
            namespace=settings.namespace,
            trigger_reason=f"manual: {reason}",
        )
    )

    return m


async def _resolve_qms_pods(
    session: AsyncSession,
    migration_id: int,
) -> tuple[str | None, str | None, str | None, str | None]:
    """Derive (source_qm, target_qm, source_pod, target_pod) from the
    Migration's MigrationStep rows. The first rewire step's payload
    captures all four.
    """
    from bcl.models.orm import QueueManager, Topology

    m = await session.get(Migration, migration_id)
    if m is None:
        return None, None, None, None

    # Find the first MigrationStep that has target_qm in its payload.
    stmt = (
        select(MigrationStep)
        .where(MigrationStep.migration_id == migration_id)
        .order_by(MigrationStep.step_index.asc())
    )
    steps = (await session.execute(stmt)).scalars().all()

    src_qm: str | None = None
    tgt_qm: str | None = None
    for s in steps:
        payload = s.payload or {}
        if payload.get("target_qm_pod_for") == "source":
            src_qm = payload.get("target_qm") or src_qm
        if payload.get("target_qm_pod_for") == "target":
            tgt_qm = payload.get("target_qm") or tgt_qm
        if src_qm and tgt_qm:
            break

    if not (src_qm and tgt_qm):
        return src_qm, tgt_qm, None, None

    # Resolve pod names for both QMs.
    src_pod = await _pod_for_qm(session, m.source_topology_id, src_qm)
    tgt_pod = await _pod_for_qm(session, m.target_topology_id, tgt_qm)
    return src_qm, tgt_qm, src_pod, tgt_pod


async def _pod_for_qm(
    session: AsyncSession,
    topology_id: int,
    qm_name: str,
) -> str | None:
    from bcl.models.orm import QueueManager

    stmt = select(QueueManager).where(
        QueueManager.topology_id == topology_id,
        QueueManager.qm_name == qm_name,
    )
    qm = (await session.execute(stmt)).scalar_one_or_none()
    return qm.pod_name if qm and qm.pod_name else None


__all__ = [
    "execute_rollback",
    "start_manual_rollback",
]
