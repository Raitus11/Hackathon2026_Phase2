"""Migration state machine — transitions and validity rules.

The state values themselves are owned by `orm.py` (single source of
truth). This module owns the *transition rules*: which states may
follow which. Code that mutates Migration.state must go through
`assert_transition` so we get a single chokepoint where invariants
are checked.

Forward path:
    PLANNED -> PROVISIONING_TARGET_QM -> VALIDATING_PRE -> REWIRING
    -> DRAIN_WAIT -> VALIDATING_DURING -> DRAINING_SOURCE
    -> VALIDATING_POST -> COMPLETED

Failure path (from any non-terminal state):
    <state> -> ROLLING_BACK -> ROLLED_BACK
    or
    <state> -> ROLLING_BACK -> ROLLBACK_FAILED  (terminal; human required)

Terminal states: COMPLETED, ROLLED_BACK, ROLLBACK_FAILED.

This module is pure (no I/O) so the state machine can be unit-tested
and reasoned about independently of the engine.

Foundations:

  - The state set + valid-edge predicate satisfy the safety property
    "no out-of-order transitions". A TLA+ spec of the same system
    would phrase the next-state relation in exactly this shape.
  - Per Lamport, "Specifying Systems" (2002), encoding the
    next-state relation in code that the engine cannot bypass is
    the executable mirror of a TLA+ Next predicate. We are not
    running TLC; we are using the discipline.
"""

from __future__ import annotations

from bcl.models.orm import MigrationState


# Each key is a state; the value is the set of states that may
# legally follow it. ROLLING_BACK is reachable from every
# non-terminal state.
_FORWARD_TRANSITIONS: dict[MigrationState, frozenset[MigrationState]] = {
    MigrationState.PLANNED: frozenset({
        MigrationState.PROVISIONING_TARGET_QM,
        MigrationState.ROLLING_BACK,
    }),
    MigrationState.PROVISIONING_TARGET_QM: frozenset({
        MigrationState.VALIDATING_PRE,
        MigrationState.ROLLING_BACK,
    }),
    MigrationState.VALIDATING_PRE: frozenset({
        MigrationState.REWIRING,
        MigrationState.ROLLING_BACK,
    }),
    MigrationState.REWIRING: frozenset({
        MigrationState.DRAIN_WAIT,
        MigrationState.ROLLING_BACK,
    }),
    MigrationState.DRAIN_WAIT: frozenset({
        MigrationState.VALIDATING_DURING,
        MigrationState.ROLLING_BACK,
    }),
    MigrationState.VALIDATING_DURING: frozenset({
        MigrationState.DRAINING_SOURCE,
        MigrationState.ROLLING_BACK,
    }),
    MigrationState.DRAINING_SOURCE: frozenset({
        MigrationState.VALIDATING_POST,
        MigrationState.ROLLING_BACK,
    }),
    MigrationState.VALIDATING_POST: frozenset({
        MigrationState.COMPLETED,
        MigrationState.ROLLING_BACK,
    }),
    # Rollback transitions
    MigrationState.ROLLING_BACK: frozenset({
        MigrationState.ROLLED_BACK,
        MigrationState.ROLLBACK_FAILED,
    }),
    # Terminal states: no outgoing transitions.
    MigrationState.COMPLETED: frozenset(),
    MigrationState.ROLLED_BACK: frozenset(),
    MigrationState.ROLLBACK_FAILED: frozenset(),
}


# Friendly progress percentage per state. Surfaces in the UI.
# Picked by where the state sits in the forward path; rollback states
# report the same percent as the last forward state for continuity.
PROGRESS_PERCENT: dict[MigrationState, int] = {
    MigrationState.PLANNED: 0,
    MigrationState.PROVISIONING_TARGET_QM: 15,
    MigrationState.VALIDATING_PRE: 25,
    MigrationState.REWIRING: 45,
    MigrationState.DRAIN_WAIT: 60,
    MigrationState.VALIDATING_DURING: 70,
    MigrationState.DRAINING_SOURCE: 85,
    MigrationState.VALIDATING_POST: 95,
    MigrationState.COMPLETED: 100,
    MigrationState.ROLLING_BACK: 50,
    MigrationState.ROLLED_BACK: 0,
    MigrationState.ROLLBACK_FAILED: 0,
}


TERMINAL_STATES: frozenset[MigrationState] = frozenset({
    MigrationState.COMPLETED,
    MigrationState.ROLLED_BACK,
    MigrationState.ROLLBACK_FAILED,
})


FAILURE_STATES: frozenset[MigrationState] = frozenset({
    MigrationState.ROLLING_BACK,
    MigrationState.ROLLED_BACK,
    MigrationState.ROLLBACK_FAILED,
})


SUCCESS_STATES: frozenset[MigrationState] = frozenset({
    MigrationState.COMPLETED,
})


def is_valid_transition(
    from_state: MigrationState, to_state: MigrationState
) -> bool:
    """True iff `from_state -> to_state` is a legal edge."""
    return to_state in _FORWARD_TRANSITIONS.get(from_state, frozenset())


def assert_transition(
    from_state: MigrationState, to_state: MigrationState
) -> None:
    """Raise ValueError if the transition is illegal. Engine chokepoint."""
    if not is_valid_transition(from_state, to_state):
        raise ValueError(
            f"illegal migration transition: {from_state.value} -> {to_state.value}. "
            f"Allowed next states from {from_state.value}: "
            f"{[s.value for s in _FORWARD_TRANSITIONS.get(from_state, frozenset())]}"
        )


def is_terminal(state: MigrationState) -> bool:
    return state in TERMINAL_STATES


def is_in_progress(state: MigrationState) -> bool:
    return state not in TERMINAL_STATES


def next_forward(state: MigrationState) -> MigrationState | None:
    """Return the next state in the forward (happy-path) order, or None
    if `state` is terminal or has no unambiguous forward successor.

    Used by the engine to know "what should we attempt after this state
    completes successfully" without hard-coding the chain everywhere.
    """
    forward_path = [
        MigrationState.PLANNED,
        MigrationState.PROVISIONING_TARGET_QM,
        MigrationState.VALIDATING_PRE,
        MigrationState.REWIRING,
        MigrationState.DRAIN_WAIT,
        MigrationState.VALIDATING_DURING,
        MigrationState.DRAINING_SOURCE,
        MigrationState.VALIDATING_POST,
        MigrationState.COMPLETED,
    ]
    try:
        idx = forward_path.index(state)
    except ValueError:
        return None
    if idx + 1 >= len(forward_path):
        return None
    return forward_path[idx + 1]


__all__ = [
    "is_valid_transition",
    "assert_transition",
    "is_terminal",
    "is_in_progress",
    "next_forward",
    "PROGRESS_PERCENT",
    "TERMINAL_STATES",
    "FAILURE_STATES",
    "SUCCESS_STATES",
]
