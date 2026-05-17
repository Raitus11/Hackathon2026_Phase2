"""RCA Assistant endpoint — root cause analysis for a migration.

GET /rca/migrations/{migration_id}
        Structured root-cause diagnosis: primary hypothesis, confidence,
        the failure event, the MQ reason code, the Lamport-ordered
        supporting evidence, contributing factors, and suggested human
        checks — plus a narrative (LLM-written when BCL_LLM_PROVIDER is a
        real provider, deterministic explainer otherwise).

Read-only. Reads Migration / MigrationStep / AuditLog; issues no MQSC;
changes no state. The only row written is the agent's own
AgentInvocation audit record.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from bcl.agents.rca import diagnose_migration
from bcl.db.session import get_session, get_session_factory

router = APIRouter(prefix="/rca", tags=["rca"])


class RcaEvidenceOut(BaseModel):
    lamport_clock: int | None
    source: str
    operation: str
    detail: str
    relevance: str


class RcaReportOut(BaseModel):
    migration_id: int
    app_id: str
    migration_state: str
    has_failure: bool
    primary_hypothesis: str
    confidence: str
    failure_event: str | None
    mq_reason_code: str | None
    mq_reason_meaning: str | None
    supporting_evidence: list[RcaEvidenceOut]
    contributing_factors: list[str]
    suggested_checks: list[str]
    narrative: str
    narrative_source: str
    references: list[str]


@router.get(
    "/migrations/{migration_id}",
    response_model=RcaReportOut,
    summary="Root cause analysis for a migration",
    description=(
        "Reads the migration's Lamport-ordered audit trail and per-step "
        "records, locates the failure event, names the MQ reason code, "
        "and produces a structured diagnosis. For a healthy migration "
        "has_failure is false and the report says so. Read-only — the "
        "RCA agent issues no MQSC and changes no state."
    ),
)
async def get_rca(
    migration_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> RcaReportOut:
    report = await diagnose_migration(
        migration_id=migration_id,
        session=session,
        session_factory=get_session_factory(),
    )
    if report is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Migration {migration_id} not found",
        )
    return RcaReportOut(
        migration_id=report.migration_id,
        app_id=report.app_id,
        migration_state=report.migration_state,
        has_failure=report.has_failure,
        primary_hypothesis=report.primary_hypothesis,
        confidence=report.confidence,
        failure_event=report.failure_event,
        mq_reason_code=report.mq_reason_code,
        mq_reason_meaning=report.mq_reason_meaning,
        supporting_evidence=[
            RcaEvidenceOut(
                lamport_clock=e.lamport_clock,
                source=e.source,
                operation=e.operation,
                detail=e.detail,
                relevance=e.relevance,
            )
            for e in report.supporting_evidence
        ],
        contributing_factors=report.contributing_factors,
        suggested_checks=report.suggested_checks,
        narrative=report.narrative,
        narrative_source=report.narrative_source,
        references=report.references,
    )
