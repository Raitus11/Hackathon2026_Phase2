"""Markov reliability analysis of the migration state machine.

The per-app migration state machine is, formally, a discrete-time
**absorbing Markov chain**:

  - Transient states: PLANNED, PROVISIONING_TARGET_QM, VALIDATING_PRE,
    REWIRING, DRAIN_WAIT, VALIDATING_DURING, DRAINING_SOURCE,
    VALIDATING_POST, ROLLING_BACK.
  - Absorbing states: COMPLETED, ROLLED_BACK, ROLLBACK_FAILED — once
    entered, never left.

This module computes the standard absorbing-chain quantities from the
fundamental matrix, plus an empirical transition estimate from the real
audit log so the theoretical model can be compared against observed runs.

References
----------
Kemeny, J. G. & Snell, J. L., "Finite Markov Chains" (Van Nostrand,
1960) — the fundamental matrix N = (I − Q)⁻¹, expected steps to
absorption t = N·1, and absorption probabilities B = N·R are the
canonical results used here, Ch. 3.

Trivedi, K. S., "Probability and Statistics with Reliability, Queuing
and Computer Science Applications" (2nd ed., Wiley, 2002), Ch. 8 —
absorbing Markov chains as the standard model for reliability /
availability analysis of systems with terminal success and failure
states.

Scope / honesty
---------------
The transition probabilities are not known a priori — a 14-day
hackathon has no historical fleet data to estimate them from. This
module therefore does two separate, clearly-labelled things:

  1. analyse_reference_chain(): the absorbing-chain mathematics on a
     *stated, explicit* transition matrix (a documented modelling
     assumption, not a measurement).
  2. estimate_from_audit(): the *empirical* maximum-likelihood
     transition frequencies counted from real AuditLog state
     transitions.

Presenting both — and the gap between them — is the honest move. The
reference chain shows the method; the empirical estimate shows the
reality; neither is dressed up as the other.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# ─────────────────────────────────────────────────────────────────────────
# State sets
# ─────────────────────────────────────────────────────────────────────────

TRANSIENT_STATES: tuple[str, ...] = (
    "PLANNED",
    "PROVISIONING_TARGET_QM",
    "VALIDATING_PRE",
    "REWIRING",
    "DRAIN_WAIT",
    "VALIDATING_DURING",
    "DRAINING_SOURCE",
    "VALIDATING_POST",
    "ROLLING_BACK",
)

ABSORBING_STATES: tuple[str, ...] = (
    "COMPLETED",
    "ROLLED_BACK",
    "ROLLBACK_FAILED",
)

ALL_STATES: tuple[str, ...] = TRANSIENT_STATES + ABSORBING_STATES


# ─────────────────────────────────────────────────────────────────────────
# Result containers
# ─────────────────────────────────────────────────────────────────────────


@dataclass
class AbsorbingChainResult:
    """Output of the absorbing-chain analysis."""

    expected_steps_to_absorption: dict[str, float]
    """t_i — expected number of transitions before absorption, per
    starting transient state. t = N·1 where N is the fundamental matrix."""

    absorption_probability: dict[str, dict[str, float]]
    """B[i][j] — probability that a run starting in transient state i is
    absorbed in absorbing state j. B = N·R."""

    fundamental_matrix_diagonal: dict[str, float]
    """N[i][i] — expected visits to state i, starting from i. The diagonal
    of the fundamental matrix; a quick read on which states dominate."""

    notes: str = ""


@dataclass
class EmpiricalEstimate:
    """Maximum-likelihood transition estimate from the real audit log."""

    transition_counts: dict[str, dict[str, int]] = field(default_factory=dict)
    transition_probabilities: dict[str, dict[str, float]] = field(
        default_factory=dict
    )
    total_transitions: int = 0
    runs_observed: int = 0
    notes: str = ""


# ─────────────────────────────────────────────────────────────────────────
# Linear algebra — small, dependency-free
# ─────────────────────────────────────────────────────────────────────────


def _identity(n: int) -> list[list[float]]:
    return [[1.0 if i == j else 0.0 for j in range(n)] for i in range(n)]


def _matsub(a: list[list[float]], b: list[list[float]]) -> list[list[float]]:
    return [[a[i][j] - b[i][j] for j in range(len(a))] for i in range(len(a))]


def _matmul(
    a: list[list[float]], b: list[list[float]]
) -> list[list[float]]:
    rows, inner, cols = len(a), len(b), len(b[0])
    out = [[0.0] * cols for _ in range(rows)]
    for i in range(rows):
        for k in range(inner):
            aik = a[i][k]
            if aik == 0.0:
                continue
            for j in range(cols):
                out[i][j] += aik * b[k][j]
    return out


def _invert(m: list[list[float]]) -> list[list[float]]:
    """Invert a square matrix by Gauss-Jordan elimination with partial
    pivoting. Small matrices only (here: 9×9). Raises ValueError if
    singular."""
    n = len(m)
    aug = [row[:] + _identity(n)[i] for i, row in enumerate(m)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(aug[r][col]))
        if abs(aug[pivot][col]) < 1e-12:
            raise ValueError("matrix is singular; cannot invert")
        aug[col], aug[pivot] = aug[pivot], aug[col]
        piv = aug[col][col]
        aug[col] = [v / piv for v in aug[col]]
        for r in range(n):
            if r == col:
                continue
            factor = aug[r][col]
            if factor == 0.0:
                continue
            aug[r] = [aug[r][k] - factor * aug[col][k] for k in range(2 * n)]
    return [row[n:] for row in aug]


# ─────────────────────────────────────────────────────────────────────────
# (1) Absorbing-chain analysis on a stated transition matrix
# ─────────────────────────────────────────────────────────────────────────


def _reference_transition_matrix() -> dict[str, dict[str, float]]:
    """An explicit, stated transition model for the migration chain.

    These are a documented modelling assumption — a plausible per-step
    success rate with a small failure branch into ROLLING_BACK — NOT a
    measurement. They exist so the absorbing-chain mathematics can be
    demonstrated end to end. estimate_from_audit() provides the
    empirical counterpart.

    Each transient state moves forward with probability `p_fwd`, or
    diverts to ROLLING_BACK with probability `p_fail`. ROLLING_BACK is
    absorbed into ROLLED_BACK (success) or ROLLBACK_FAILED (failure).
    """
    p_fwd = 0.97
    p_fail = 0.03
    forward = {
        "PLANNED": "PROVISIONING_TARGET_QM",
        "PROVISIONING_TARGET_QM": "VALIDATING_PRE",
        "VALIDATING_PRE": "REWIRING",
        "REWIRING": "DRAIN_WAIT",
        "DRAIN_WAIT": "VALIDATING_DURING",
        "VALIDATING_DURING": "DRAINING_SOURCE",
        "DRAINING_SOURCE": "VALIDATING_POST",
        "VALIDATING_POST": "COMPLETED",
    }
    m: dict[str, dict[str, float]] = {}
    for s in TRANSIENT_STATES:
        row = {t: 0.0 for t in ALL_STATES}
        if s == "ROLLING_BACK":
            # Rollback resolves: mostly succeeds, rarely gets stuck.
            row["ROLLED_BACK"] = 0.95
            row["ROLLBACK_FAILED"] = 0.05
        else:
            row[forward[s]] = p_fwd
            row["ROLLING_BACK"] = p_fail
        m[s] = row
    return m


def analyse_reference_chain() -> AbsorbingChainResult:
    """Compute absorbing-chain quantities for the stated reference model.

    Uses the fundamental matrix N = (I − Q)⁻¹ where Q is the
    transient→transient block of the transition matrix (Kemeny & Snell
    1960, Ch. 3):

      - expected steps to absorption  t = N · 1
      - absorption probabilities      B = N · R   (R = transient→absorbing)
    """
    P = _reference_transition_matrix()
    n = len(TRANSIENT_STATES)
    t_index = {s: i for i, s in enumerate(TRANSIENT_STATES)}
    a_index = {s: i for i, s in enumerate(ABSORBING_STATES)}

    # Q: transient → transient
    Q = [[0.0] * n for _ in range(n)]
    for s in TRANSIENT_STATES:
        for t in TRANSIENT_STATES:
            Q[t_index[s]][t_index[t]] = P[s][t]

    # R: transient → absorbing
    R = [[0.0] * len(ABSORBING_STATES) for _ in range(n)]
    for s in TRANSIENT_STATES:
        for a in ABSORBING_STATES:
            R[t_index[s]][a_index[a]] = P[s][a]

    # N = (I - Q)^-1
    N = _invert(_matsub(_identity(n), Q))

    # t = N · 1
    t_vec = [sum(N[i]) for i in range(n)]
    expected_steps = {
        s: round(t_vec[t_index[s]], 4) for s in TRANSIENT_STATES
    }

    # B = N · R
    B = _matmul(N, R)
    absorption = {
        s: {
            a: round(B[t_index[s]][a_index[a]], 6)
            for a in ABSORBING_STATES
        }
        for s in TRANSIENT_STATES
    }

    diag = {s: round(N[t_index[s]][t_index[s]], 4) for s in TRANSIENT_STATES}

    return AbsorbingChainResult(
        expected_steps_to_absorption=expected_steps,
        absorption_probability=absorption,
        fundamental_matrix_diagonal=diag,
        notes=(
            "Reference model. Transition probabilities are a stated "
            "modelling assumption (p_forward=0.97, p_fail=0.03 per step), "
            "not a measurement. N = (I-Q)^-1 per Kemeny & Snell (1960). "
            "See estimate_from_audit() for the empirical counterpart."
        ),
    )


# ─────────────────────────────────────────────────────────────────────────
# (2) Empirical transition estimate from the real audit log
# ─────────────────────────────────────────────────────────────────────────


def estimate_from_audit(
    state_transitions: list[tuple[str, str]],
) -> EmpiricalEstimate:
    """Maximum-likelihood transition-probability estimate from observed
    state transitions.

    Parameters
    ----------
    state_transitions
        A list of (from_state, to_state) pairs, in order, harvested from
        AuditLog rows whose operation is a migration state transition.
        The caller is responsible for extracting these from the audit
        log (state_before / state_after columns).

    Returns
    -------
    EmpiricalEstimate
        Counts and row-normalised probabilities. With a hackathon's
        handful of runs this is a low-sample estimate — the `notes`
        field says so. It is the honest empirical companion to
        analyse_reference_chain(); it is not dressed up as a converged
        measurement.
    """
    counts: dict[str, dict[str, int]] = {}
    for frm, to in state_transitions:
        counts.setdefault(frm, {})
        counts[frm][to] = counts[frm].get(to, 0) + 1

    probs: dict[str, dict[str, float]] = {}
    for frm, row in counts.items():
        total = sum(row.values())
        probs[frm] = {
            to: round(c / total, 6) for to, c in row.items()
        } if total else {}

    runs = sum(
        1
        for _frm, to in state_transitions
        if to in ABSORBING_STATES
    )

    sample_note = (
        f"Empirical MLE from {len(state_transitions)} observed "
        f"transition(s) across {runs} absorbed run(s). "
    )
    if runs < 10:
        sample_note += (
            "LOW SAMPLE — with fewer than 10 absorbed runs this estimate "
            "is indicative only, not a converged transition matrix. "
            "Reported for honest comparison against the reference model."
        )

    return EmpiricalEstimate(
        transition_counts=counts,
        transition_probabilities=probs,
        total_transitions=len(state_transitions),
        runs_observed=runs,
        notes=sample_note,
    )


__all__ = [
    "TRANSIENT_STATES",
    "ABSORBING_STATES",
    "ALL_STATES",
    "AbsorbingChainResult",
    "EmpiricalEstimate",
    "analyse_reference_chain",
    "estimate_from_audit",
]
