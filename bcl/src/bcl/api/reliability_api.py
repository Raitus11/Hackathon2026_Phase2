"""Reliability analysis endpoints — absorbing Markov chain on the
migration state machine.

GET /reliability/markov   absorbing-chain analysis + empirical estimate

The per-app migration state machine is an absorbing Markov chain. This
endpoint exposes:

  1. The absorbing-chain mathematics (fundamental matrix N = (I-Q)^-1,
     expected steps to absorption, absorption probabilities) computed on
     an explicit, stated reference transition model.
  2. An empirical maximum-likelihood transition estimate counted from
     the real audit log's state transitions.

Both are returned together, each clearly labelled. The reference model's
probabilities are a documented modelling assumption; the empirical
estimate is a (low-sample) measurement. The endpoint does not blur the
two — see bcl.analysis.reliability for the rationale.

References: Kemeny & Snell, "Finite Markov Chains" (1960), Ch. 3;
Trivedi, "Probability and Statistics with Reliability, Queuing and
Computer Science Applications" (2nd ed., 2002), Ch. 8.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bcl.analysis.reliability import (
    ABSORBING_STATES,
    TRANSIENT_STATES,
    analyse_reference_chain,
    estimate_from_audit,
)
from bcl.db.session import get_session
from bcl.models.orm import AuditLog

router = APIRouter(prefix="/reliability", tags=["reliability"])


class MarkovAnalysisOut(BaseModel):
    """GET /reliability/markov response."""

    transient_states: list[str]
    absorbing_states: list[str]

    reference_model: dict[str, Any]
    """Absorbing-chain analysis on the stated reference transition
    matrix: expected_steps_to_absorption, absorption_probability,
    fundamental_matrix_diagonal, notes."""

    empirical_estimate: dict[str, Any]
    """Maximum-likelihood transition estimate from real audit-log state
    transitions: transition_counts, transition_probabilities,
    total_transitions, runs_observed, notes."""

    method_reference: str


async def _extract_state_transitions(
    session: AsyncSession,
) -> list[tuple[str, str]]:
    """Harvest (from_state, to_state) pairs from the audit log.

    Migration state transitions are recorded in AuditLog rows whose
    state_before / state_after columns are populated. We read those
    columns and pull the migration state out of each.
    """
    rows = (
        await session.execute(
            select(AuditLog)
            .where(AuditLog.state_before.is_not(None))
            .where(AuditLog.state_after.is_not(None))
            .order_by(AuditLog.lamport_clock)
        )
    ).scalars().all()

    transitions: list[tuple[str, str]] = []
    valid = set(TRANSIENT_STATES) | set(ABSORBING_STATES)
    for r in rows:
        before = r.state_before or {}
        after = r.state_after or {}
        # state_before / state_after are JSON dicts; the migration state
        # is stored under a 'state' key by the migration engine.
        frm = before.get("state") if isinstance(before, dict) else None
        to = after.get("state") if isinstance(after, dict) else None
        if frm in valid and to in valid and frm != to:
            transitions.append((frm, to))
    return transitions


@router.get(
    "/markov",
    response_model=MarkovAnalysisOut,
    summary="Absorbing Markov chain analysis of the migration state machine",
    description=(
        "Returns the absorbing-chain mathematics (fundamental matrix, "
        "expected steps to absorption, absorption probabilities) for the "
        "migration state machine, plus an empirical transition estimate "
        "counted from the real audit log.\n\n"
        "The reference model's transition probabilities are an explicit "
        "modelling assumption; the empirical estimate is a measurement "
        "from observed runs. With a hackathon's small number of runs the "
        "empirical estimate is low-sample — its `notes` field says so."
    ),
)
async def markov_analysis(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> MarkovAnalysisOut:
    ref = analyse_reference_chain()
    transitions = await _extract_state_transitions(session)
    emp = estimate_from_audit(transitions)

    return MarkovAnalysisOut(
        transient_states=list(TRANSIENT_STATES),
        absorbing_states=list(ABSORBING_STATES),
        reference_model={
            "expected_steps_to_absorption": ref.expected_steps_to_absorption,
            "absorption_probability": ref.absorption_probability,
            "fundamental_matrix_diagonal": ref.fundamental_matrix_diagonal,
            "notes": ref.notes,
        },
        empirical_estimate={
            "transition_counts": emp.transition_counts,
            "transition_probabilities": emp.transition_probabilities,
            "total_transitions": emp.total_transitions,
            "runs_observed": emp.runs_observed,
            "notes": emp.notes,
        },
        method_reference=(
            "Kemeny & Snell, Finite Markov Chains (1960), Ch. 3; "
            "Trivedi, Probability and Statistics with Reliability, "
            "Queuing and Computer Science Applications (2002), Ch. 8."
        ),
    )
