"""Export & summary endpoints — read-only evidence and at-a-glance views.

GET /migrations/{id}/mqsc-script   — the MQSC this migration executed,
                                     as a downloadable .mqsc text file.
GET /exports/audit.csv             — the Lamport-ordered audit log as CSV.
GET /healthz/summary               — system state at a glance (counts).
GET /migrations/{id}/evidence      — a .zip evidence bundle: the MQSC
                                     script, the migration's audit
                                     subset as CSV, and a summary.

All four are strictly read-only. They read existing DB rows and stream
text/zip back. They issue no MQSC, run no `oc` command, change no
state, and write nothing to the database.

Identifier hygiene: the MQSC script and evidence bundle are scrubbed of
the Wells Fargo user id pattern before download (see _scrub).
"""

from __future__ import annotations

import csv
import io
import re
import zipfile
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import Response, StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bcl.db.session import get_session
from bcl.models.orm import (
    AgentInvocation,
    AuditLog,
    Migration,
    MigrationState,
    MigrationStep,
)

router = APIRouter(tags=["exports"])


# ─────────────────────────────────────────────────────────────────────────
# Identifier hygiene
# ─────────────────────────────────────────────────────────────────────────

# Wells Fargo user ids look like a letter followed by digits (e.g. U######).
# Any such token in exported MQSC / evidence is replaced before download.
_UID_PATTERN = re.compile(r"\b[Uu]\d{6,}\b")


def _scrub(text: str) -> str:
    """Replace any WF-user-id-shaped token with a placeholder."""
    return _UID_PATTERN.sub("<UID>", text)


# ─────────────────────────────────────────────────────────────────────────
# Audit CSV — shared column schema
#
# One schema, used by both the global audit export and the per-migration
# evidence bundle, so the two never drift. The columns are everything an
# operator or judge needs to read the trail without opening the BCL: the
# Lamport clock and wall clock, who acted, what operation, which app and
# QM, the migration state transition, the MQSC command itself (pulled out
# of request_payload), success / rollback flags, timing, and any error.
# ─────────────────────────────────────────────────────────────────────────

_AUDIT_CSV_HEADER = [
    "lamport_clock",
    "wall_clock",
    "correlation_id",
    "actor",
    "operation",
    "app_id",
    "qm_name",
    "state_before",
    "state_after",
    "mqsc_command",
    "success",
    "is_rollback",
    "duration_ms",
    "error_message",
]


def _state_str(value: object) -> str:
    """Render a state column. state_before/after may be a plain string or
    a small JSON dict like {"state": "REWIRING"} — handle both."""
    if value is None:
        return ""
    if isinstance(value, dict):
        return str(value.get("state", value))
    return str(value)


def _mqsc_from_payload(payload: object) -> str:
    """Pull the MQSC command text out of an audit row's request_payload,
    if present. State-changing MQSC ops record it there. Best-effort —
    returns '' when the row carries no MQSC."""
    if not isinstance(payload, dict):
        return ""
    for key in ("mqsc_text", "mqsc", "command"):
        v = payload.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def _audit_csv_row(r: AuditLog) -> list[object]:
    """One enriched audit row, matching _AUDIT_CSV_HEADER."""
    return [
        r.lamport_clock,
        r.wall_clock.isoformat() if r.wall_clock else "",
        r.correlation_id,
        r.actor,
        r.operation.value,
        r.app_id or "",
        r.qm_name or "",
        _state_str(r.state_before),
        _state_str(r.state_after),
        _scrub(_mqsc_from_payload(r.request_payload)),
        r.success,
        r.is_rollback,
        r.duration_ms if r.duration_ms is not None else "",
        _scrub(r.error_message or ""),
    ]


# ─────────────────────────────────────────────────────────────────────────
# 1. MQSC script download
# ─────────────────────────────────────────────────────────────────────────


@router.get(
    "/migrations/{migration_id}/mqsc-script",
    summary="Download the MQSC this migration executed (.mqsc)",
    description=(
        "Returns the MQSC commands recorded on this migration's "
        "MigrationStep rows, in execution order, as a downloadable "
        "`.mqsc` text file. This is the commands as actually recorded — "
        "audit evidence, not a regenerated script. Read-only."
    ),
)
async def download_mqsc_script(
    migration_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Response:
    migration = await session.get(Migration, migration_id)
    if migration is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Migration {migration_id} not found",
        )
    steps = (
        await session.execute(
            select(MigrationStep)
            .where(MigrationStep.migration_id == migration_id)
            .order_by(MigrationStep.step_index)
        )
    ).scalars().all()

    body = _render_mqsc_script(migration, list(steps))
    filename = f"migration-{migration_id}-{migration.app_id}.mqsc".replace(
        "/", "_"
    )
    return Response(
        content=body,
        media_type="text/plain",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"'
        },
    )


def _render_mqsc_script(
    migration: Migration, steps: list[MigrationStep]
) -> str:
    """Render the recorded MQSC steps as a commented .mqsc script."""
    lines: list[str] = [
        "* ───────────────────────────────────────────────────────────",
        f"* MQSC executed for migration #{migration.id} — app {migration.app_id}",
        f"* Migration state: {migration.state.value}",
        f"* Exported: {datetime.now(UTC).isoformat()}",
        "* Source: the migration's recorded MigrationStep rows (audit",
        "*         evidence — the commands as actually executed).",
        "* ───────────────────────────────────────────────────────────",
        "",
    ]
    if not steps:
        lines.append("* (no steps recorded for this migration)")
        return _scrub("\n".join(lines)) + "\n"

    for s in steps:
        mqsc = (s.payload or {}).get("mqsc_text", "")
        status_mark = (
            "ok" if s.succeeded
            else "FAILED" if s.succeeded is False
            else "pending"
        )
        lines.append(
            f"* step {s.step_index} [{status_mark}] {s.audit_op.value} — "
            f"{s.description}"
        )
        if mqsc:
            lines.append(mqsc)
        if s.succeeded is False and s.error_message:
            lines.append(f"*   error: {s.error_message}")
        lines.append("")
    return _scrub("\n".join(lines)) + "\n"


# ─────────────────────────────────────────────────────────────────────────
# 2. Audit log CSV export
# ─────────────────────────────────────────────────────────────────────────


@router.get(
    "/exports/audit.csv",
    summary="Download the audit log as CSV (Lamport-ordered)",
    description=(
        "Streams the audit log as CSV, ordered by Lamport clock. "
        "Optional ?app_id= filter. Read-only."
    ),
)
async def export_audit_csv(
    session: Annotated[AsyncSession, Depends(get_session)],
    app_id: str | None = None,
    limit: int = 5000,
) -> StreamingResponse:
    stmt = select(AuditLog).order_by(AuditLog.lamport_clock.asc()).limit(limit)
    if app_id is not None:
        stmt = stmt.where(AuditLog.app_id == app_id)
    rows = (await session.execute(stmt)).scalars().all()

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(_AUDIT_CSV_HEADER)
    for r in rows:
        writer.writerow(_audit_csv_row(r))
    csv_text = buf.getvalue()
    fname = "audit-log.csv" if app_id is None else f"audit-{app_id}.csv".replace(
        "/", "_"
    )
    return StreamingResponse(
        iter([csv_text]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


# ─────────────────────────────────────────────────────────────────────────
# 3. Health / state summary
# ─────────────────────────────────────────────────────────────────────────


@router.get(
    "/healthz/summary",
    summary="System state at a glance",
    description=(
        "Aggregate counts: migrations by state, audit log size, agent "
        "invocation count, and the most recent failure if any. Read-only."
    ),
)
async def health_summary(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict:
    # Migrations by state.
    state_rows = (
        await session.execute(
            select(Migration.state, func.count())
            .group_by(Migration.state)
        )
    ).all()
    by_state = {state.value: count for state, count in state_rows}
    total_migrations = sum(by_state.values())

    completed = by_state.get(MigrationState.COMPLETED.value, 0)
    failure_states = [
        s.value
        for s in MigrationState
        if "FAIL" in s.value or s.value == "ROLLED_BACK"
    ]
    failed = sum(by_state.get(s, 0) for s in failure_states)
    in_flight = total_migrations - completed - failed

    audit_count = (
        await session.execute(select(func.count()).select_from(AuditLog))
    ).scalar_one()
    agent_count = (
        await session.execute(
            select(func.count()).select_from(AgentInvocation)
        )
    ).scalar_one()

    # Most recent failed audit row, if any.
    last_failure_row = (
        await session.execute(
            select(AuditLog)
            .where(AuditLog.success.is_(False))
            .order_by(AuditLog.lamport_clock.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    last_failure = None
    if last_failure_row is not None:
        last_failure = {
            "lamport_clock": last_failure_row.lamport_clock,
            "operation": last_failure_row.operation.value,
            "app_id": last_failure_row.app_id,
            "error_message": _scrub(last_failure_row.error_message or ""),
        }

    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "migrations": {
            "total": total_migrations,
            "completed": completed,
            "in_flight": in_flight,
            "failed_or_rolled_back": failed,
            "by_state": by_state,
        },
        "audit_log_entries": audit_count,
        "agent_invocations": agent_count,
        "last_failure": last_failure,
    }


# ─────────────────────────────────────────────────────────────────────────
# 4. Evidence bundle (ZIP)
# ─────────────────────────────────────────────────────────────────────────


@router.get(
    "/migrations/{migration_id}/evidence",
    summary="Download a migration evidence bundle (.zip)",
    description=(
        "A ZIP containing the migration's executed MQSC script, its "
        "Lamport-ordered audit subset as CSV, and a plain-text summary. "
        "Generated on the fly from recorded DB rows. Read-only."
    ),
)
async def download_evidence_bundle(
    migration_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Response:
    migration = await session.get(Migration, migration_id)
    if migration is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Migration {migration_id} not found",
        )

    steps = (
        await session.execute(
            select(MigrationStep)
            .where(MigrationStep.migration_id == migration_id)
            .order_by(MigrationStep.step_index)
        )
    ).scalars().all()
    # Audit rows for THIS migration: the rows its steps point at via
    # audit_log_id. Scoping by app_id would leak in other migrations of
    # the same app.
    audit_ids = [s.audit_log_id for s in steps if s.audit_log_id is not None]
    if audit_ids:
        audit = (
            await session.execute(
                select(AuditLog)
                .where(AuditLog.id.in_(audit_ids))
                .order_by(AuditLog.lamport_clock.asc())
            )
        ).scalars().all()
    else:
        audit = []

    # MQSC script.
    mqsc_script = _render_mqsc_script(migration, list(steps))

    # Audit CSV — same enriched schema as the global export.
    abuf = io.StringIO()
    aw = csv.writer(abuf)
    aw.writerow(_AUDIT_CSV_HEADER)
    for r in audit:
        aw.writerow(_audit_csv_row(r))

    # Summary.
    n_steps = len(steps)
    n_failed = sum(1 for s in steps if s.succeeded is False)
    summary = _scrub(
        "\n".join(
            [
                f"Evidence bundle — migration #{migration.id}",
                f"App:            {migration.app_id}",
                f"State:          {migration.state.value}",
                f"Steps recorded: {n_steps}  (failed: {n_failed})",
                f"Audit entries:  {len(audit)}",
                f"Started:        {migration.started_at}",
                f"Completed:      {migration.completed_at}",
                f"Exported:       {datetime.now(UTC).isoformat()}",
                "",
                "Contents of this bundle:",
                "  migration.mqsc  — the MQSC executed, in order",
                "  audit.csv       — the Lamport-ordered audit subset",
                "  summary.txt     — this file",
                "",
                "Read-only export. Generated from recorded BCL database",
                "rows; no MQSC was issued and no state changed to produce",
                "this bundle.",
            ]
        )
    )

    zbuf = io.BytesIO()
    with zipfile.ZipFile(zbuf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("migration.mqsc", mqsc_script)
        zf.writestr("audit.csv", abuf.getvalue())
        zf.writestr("summary.txt", summary + "\n")
    zbuf.seek(0)

    fname = f"evidence-migration-{migration_id}-{migration.app_id}.zip".replace(
        "/", "_"
    )
    return Response(
        content=zbuf.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )
