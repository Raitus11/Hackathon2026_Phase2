"""Drain detection — Little's Law prediction + zero-window polling.

After REWIRING, the source-side QLOCAL no longer accepts new messages
(producers are now PUTting into the new QREMOTE, which transmits via
the bridge XMITQ to the target). Any messages already on the source
QLOCAL must be drained by existing consumers before we can safely
DELETE the QLOCAL.

The zero-window condition (after Lamport-style careful-distributed-
system practice): we require three consecutive 100-500ms-spaced polls
to all observe:

    CURDEPTH == 0  AND  IPPROCS == 0  AND  OPPROCS == 0

A single zero reading is not enough — between two polls a putting
process could arrive, deposit, and be drained, giving us a false-true
zero reading. The 3-poll window over (~1s real time) is the smallest
that's empirically robust on the MQ container.

Little's Law gives us the *prediction* we surface in the UI:

    T_drain ≈ L_0 / μ

where L_0 is the observed depth at rewire-time and μ is the consumer
service rate (msg/sec). We measure μ from the depth-decrease rate
during the first ~2 seconds of polling; it's not a strict steady-state
service rate but it's the operationally useful estimate of "how fast
is this queue actually emptying right now".

Reference: Little, J. D. C. (1961). "A Proof for the Queuing Formula:
L = λW." Operations Research, 9(3), 383-387. Little's relationship
holds for any work-conserving queueing system in steady-state; ours
is not strictly steady (λ_in -> 0 after rewire) but the average-time-
in-system interpretation gives a useful drain-time estimate.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass
from typing import Literal

from bcl.provisioning.mq_client import MqClient

logger = logging.getLogger("bcl.migration.drain")


# ─────────────────────────────────────────────────────────────────────────
# Parsers — DISPLAY QLOCAL output
# ─────────────────────────────────────────────────────────────────────────


_CURDEPTH_RE = re.compile(r"\bCURDEPTH\((\d+)\)")
_IPPROCS_RE = re.compile(r"\bIPPROCS\((\d+)\)")
_OPPROCS_RE = re.compile(r"\bOPPROCS\((\d+)\)")
_AMQ_NOT_FOUND_RE = re.compile(r"\bAMQ8147\w*\b")


@dataclass
class QueueProbe:
    """One snapshot of a queue's depth + reader/writer counts."""

    queue_name: str
    depth: int | None
    ipprocs: int | None
    opprocs: int | None
    raw_stdout: str
    raw_stderr: str
    error_kind: Literal[
        "ok", "queue_not_found", "mqsc_error", "no_attrs_in_output",
    ]

    @property
    def is_zero(self) -> bool:
        """The zero-window condition for this single probe."""
        return (
            self.depth == 0
            and self.ipprocs == 0
            and self.opprocs == 0
        )


async def probe_queue(
    client: MqClient,
    *,
    qm_name: str,
    pod_name: str,
    queue_name: str,
    namespace: str,
    timeout: float = 10.0,
) -> QueueProbe:
    """DISPLAY QLOCAL(<q>) CURDEPTH IPPROCS OPPROCS — one round trip.

    Structured failure reporting matches the drain-engine's needs:
    we must distinguish "queue truly empty" from "queue gone" from
    "QM unreachable" — they need different remediation.
    """
    result = await client.apply_mqsc(
        qm_name=qm_name,
        pod_name=pod_name,
        mqsc_text=(
            f"DISPLAY QLOCAL('{queue_name}') CURDEPTH IPPROCS OPPROCS"
        ),
        namespace=namespace,
        timeout=timeout,
    )
    out = result.raw_stdout or ""
    err = result.raw_stderr or ""

    if result.exit_code != 0 or _AMQ_NOT_FOUND_RE.search(out + err):
        kind: Literal[
            "queue_not_found", "mqsc_error",
        ] = (
            "queue_not_found"
            if _AMQ_NOT_FOUND_RE.search(out + err)
            else "mqsc_error"
        )
        return QueueProbe(
            queue_name=queue_name,
            depth=None, ipprocs=None, opprocs=None,
            raw_stdout=out, raw_stderr=err, error_kind=kind,
        )

    md = _CURDEPTH_RE.search(out)
    mi = _IPPROCS_RE.search(out)
    mo = _OPPROCS_RE.search(out)
    if not (md and mi and mo):
        return QueueProbe(
            queue_name=queue_name,
            depth=int(md.group(1)) if md else None,
            ipprocs=int(mi.group(1)) if mi else None,
            opprocs=int(mo.group(1)) if mo else None,
            raw_stdout=out, raw_stderr=err,
            error_kind="no_attrs_in_output",
        )
    return QueueProbe(
        queue_name=queue_name,
        depth=int(md.group(1)),
        ipprocs=int(mi.group(1)),
        opprocs=int(mo.group(1)),
        raw_stdout=out, raw_stderr=err, error_kind="ok",
    )


# ─────────────────────────────────────────────────────────────────────────
# Drain prediction — Little's Law
# ─────────────────────────────────────────────────────────────────────────


@dataclass
class DrainPrediction:
    """Drain-time prediction. Surfaces in the UI as the Little's Law widget.

    Fields:
        queue_name: which queue this is for
        l0: observed depth at start of drain wait
        mu_msgs_per_sec: observed consumer service rate; None until measured
        predicted_seconds: L_0 / μ; None until μ is known or if L_0 = 0
        formula: human-readable formula for the UI
        reference: citation surfaced in the UI for academic honesty
    """

    queue_name: str
    l0: int
    mu_msgs_per_sec: float | None
    predicted_seconds: float | None
    formula: str = "T_drain ≈ L_0 / μ  (Little 1961)"
    reference: str = (
        "Little, J. D. C. (1961). "
        '"A Proof for the Queuing Formula: L = λW". '
        "Operations Research, 9(3), 383-387."
    )

    def as_dict(self) -> dict[str, object]:
        return {
            "queue_name": self.queue_name,
            "l0": self.l0,
            "mu_msgs_per_sec": self.mu_msgs_per_sec,
            "predicted_seconds": self.predicted_seconds,
            "formula": self.formula,
            "reference": self.reference,
        }


def predict_drain_time(*, l0: int, mu: float | None) -> float | None:
    """Apply Little's Law given baseline depth and service rate.

    Returns None if μ is unknown or zero (cannot divide). The engine
    treats "unknown" as "drain budget capped by drain_wait_timeout_seconds".
    """
    if l0 == 0:
        return 0.0
    if mu is None or mu <= 0:
        return None
    return l0 / mu


# ─────────────────────────────────────────────────────────────────────────
# Drain loop — zero-window polling
# ─────────────────────────────────────────────────────────────────────────


@dataclass
class DrainOutcome:
    """Result of waiting for a queue to drain."""

    queue_name: str
    drained: bool
    final_depth: int | None
    polls_taken: int
    wall_duration_seconds: float
    initial_depth: int
    measured_mu: float | None
    """msg/sec consumer rate, measured from depth-decrease."""

    error_kind: str | None
    """None if drained == True. Otherwise one of: 'timeout',
    'queue_not_found', 'mqsc_error', 'unstable'."""

    history: list[dict[str, object]]
    """Per-poll snapshot history. Used by audit log + UI timeline."""


async def wait_for_drain(
    client: MqClient,
    *,
    qm_name: str,
    pod_name: str,
    queue_name: str,
    namespace: str,
    timeout_seconds: float,
    poll_interval_ms: int,
    zero_window_polls: int,
) -> DrainOutcome:
    """Poll a queue until the zero-window condition holds or timeout.

    `zero_window_polls`: number of consecutive zero readings required
        (settings.drain_zero_window_polls — typically 3).
    `poll_interval_ms`: sleep between polls (settings.drain_poll_interval_ms —
        typically 500ms).
    `timeout_seconds`: total wall-clock budget (settings.drain_wait_timeout_seconds —
        typically 300s).

    Measures μ from the first ~2 seconds of polling so the UI gets a
    Little's Law prediction even before drain completes.
    """
    poll_interval_s = poll_interval_ms / 1000.0
    t0 = time.monotonic()
    deadline = t0 + timeout_seconds

    history: list[dict[str, object]] = []
    consecutive_zero = 0
    polls = 0
    initial_depth: int | None = None
    initial_t: float | None = None
    measured_mu: float | None = None
    last_probe: QueueProbe | None = None

    while time.monotonic() < deadline:
        polls += 1
        probe = await probe_queue(
            client,
            qm_name=qm_name, pod_name=pod_name,
            queue_name=queue_name, namespace=namespace,
        )
        last_probe = probe
        now = time.monotonic() - t0

        snapshot: dict[str, object] = {
            "poll": polls,
            "t_seconds": round(now, 3),
            "depth": probe.depth,
            "ipprocs": probe.ipprocs,
            "opprocs": probe.opprocs,
            "error_kind": probe.error_kind,
        }
        history.append(snapshot)

        if probe.error_kind == "queue_not_found":
            # The queue is already gone. Treat as drained — there's
            # nothing left to wait for. This happens if rewire+delete
            # races with this drain (engine bug) or operator pre-cleanup.
            return DrainOutcome(
                queue_name=queue_name,
                drained=True,
                final_depth=0,
                polls_taken=polls,
                wall_duration_seconds=round(now, 3),
                initial_depth=initial_depth or 0,
                measured_mu=measured_mu,
                error_kind=None,
                history=history,
            )

        if probe.error_kind != "ok":
            # MQSC failure or unparseable output. Surface and exit.
            return DrainOutcome(
                queue_name=queue_name,
                drained=False,
                final_depth=None,
                polls_taken=polls,
                wall_duration_seconds=round(now, 3),
                initial_depth=initial_depth or 0,
                measured_mu=measured_mu,
                error_kind=probe.error_kind,
                history=history,
            )

        # Record initial baseline + measure μ.
        if initial_depth is None:
            initial_depth = probe.depth or 0
            initial_t = now
        elif (
            measured_mu is None
            and probe.depth is not None
            and initial_depth > 0
            and (now - (initial_t or 0)) >= 1.5
            and probe.depth < initial_depth
        ):
            elapsed = now - (initial_t or 0)
            consumed = initial_depth - probe.depth
            if elapsed > 0:
                measured_mu = consumed / elapsed

        # Zero-window logic
        if probe.is_zero:
            consecutive_zero += 1
            if consecutive_zero >= zero_window_polls:
                return DrainOutcome(
                    queue_name=queue_name,
                    drained=True,
                    final_depth=0,
                    polls_taken=polls,
                    wall_duration_seconds=round(now, 3),
                    initial_depth=initial_depth or 0,
                    measured_mu=measured_mu,
                    error_kind=None,
                    history=history,
                )
        else:
            consecutive_zero = 0

        await asyncio.sleep(poll_interval_s)

    # Timed out
    return DrainOutcome(
        queue_name=queue_name,
        drained=False,
        final_depth=last_probe.depth if last_probe else None,
        polls_taken=polls,
        wall_duration_seconds=round(time.monotonic() - t0, 3),
        initial_depth=initial_depth or 0,
        measured_mu=measured_mu,
        error_kind="timeout",
        history=history,
    )


__all__ = [
    "QueueProbe",
    "DrainPrediction",
    "DrainOutcome",
    "predict_drain_time",
    "probe_queue",
    "wait_for_drain",
]
