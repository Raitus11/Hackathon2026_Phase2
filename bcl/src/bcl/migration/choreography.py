"""Migration choreography — pure MQSC builders per state transition.

I/O-free. Given an app, a source topology, and a target topology,
this module computes the MQSC commands required to move that app
from source to target.

Mirrors the structure of `provisioning.mqsc_derivation` so the audit
log shows the same shape of commands regardless of whether they're
emitted by the realize engine or the migration engine. The key
difference is *scope*: realize plans cover an entire QM; migration
plans cover *one app's flows* and explicitly bridge source and target.

The migration choreography per transition:

  PROVISIONING_TARGET_QM
    No MQSC. The target QM was already provisioned + realized by
    POST /topologies/{target}/provision and /realize-mq-objects.
    The engine validates the target QM exists and is ready.

  VALIDATING_PRE
    DISPLAY commands only — read-only probes. Capture baseline depths
    on every queue the app owns on the source QM. No state mutation.

  REWIRING
    The substantive forward action. On the SOURCE QM, for each of the
    app's source-resident QLOCAL queues:
      DELETE QLOCAL(<name>)
      DEFINE QREMOTE(<name>) RNAME(<same-name-on-target>)
                              RQMNAME(<target-qm>) XMITQ(<bridge-xmitq>)
    The bridge XMITQ + SDR + RCVR pair on (source <-> target) is
    defined as well if it doesn't already exist. The RCVR is defined
    on the TARGET QM; everything else lives on source. This module
    produces a structured per-QM command list; the engine routes each
    command to the right pod.

    NOTE on rollback_text: every REWIRING command has an inverse
    captured at plan-derivation time. The rollback engine simply
    walks MigrationStep rows in reverse step_index order and
    executes rollback_payload['mqsc_text']. Per-app locality of
    rollback is a consequence: a Migration's steps cover only that
    Migration's app, so reversing them touches nothing else.

  DRAIN_WAIT
    No MQSC. The engine polls DISPLAY QLOCAL(...) CURDEPTH on the
    source QM's affected queues until the zero-window condition
    holds (see drain.py).

  VALIDATING_DURING
    A canary amqsput+amqsget round-trip through the bridge path.
    Implemented in the engine; this module supplies the MQSC probe
    text.

  DRAINING_SOURCE
    A final drain pass on the XMITQ bridges. Same primitive as
    DRAIN_WAIT but targets the source XMITQs instead of the app's
    front-line queues.

  VALIDATING_POST
    Same as VALIDATING_DURING but with stricter latency budget.

References:

  - IBM MQ "Transmission queues" — XMITQ semantics:
    https://www.ibm.com/docs/en/ibm-mq/9.4?topic=queues-transmission
  - IBM MQ "Remote queue objects" — QREMOTE semantics:
    https://www.ibm.com/docs/en/ibm-mq/9.4?topic=qm-remote-queue-objects
  - Little, J. D. C. (1961). "A Proof for the Queuing Formula: L = λW".
    Operations Research, 9(3) — drives the DRAIN_WAIT prediction.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from bcl.models.api import FlowSpec
from bcl.models.orm import AuditOperation


# ─────────────────────────────────────────────────────────────────────────
# Bridge naming
# ─────────────────────────────────────────────────────────────────────────


def bridge_channel_name(source_qm: str, target_qm: str) -> str:
    """Canonical SDR/RCVR pair name for the source -> target bridge.

    MQSC requires the SDR and the matching RCVR to share a name. We
    use `<SRC>.TO.<TGT>` which mirrors the existing realize-engine's
    output for cross-QM channels and stays within the 48-char MQ
    name limit for all our QM names.
    """
    name = f"{source_qm}.TO.{target_qm}"
    if len(name) > 48:
        # Pathological but defensive: truncate the source side, keep
        # target intact (the demo's QM names are well under the limit
        # so this branch should not trigger for our data).
        remaining = 48 - len(f".TO.{target_qm}")
        name = f"{source_qm[:remaining]}.TO.{target_qm}"
    return name


def bridge_xmitq_name(target_qm: str) -> str:
    """XMITQ on the source side that the bridge SDR reads from.

    Naming follows existing realize-engine convention: `<TGT>.XMIT`.
    """
    return f"{target_qm}.XMIT"


# ─────────────────────────────────────────────────────────────────────────
# Per-command record (mirrors mqsc_derivation.MqscCommand shape)
# ─────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class MigrationMqscCommand:
    """One MQSC command in a migration plan.

    `target_qm_pod_for` indicates which QM this command runs on:
      "source" -> the source QM pod
      "target" -> the target QM pod
    The engine resolves to a pod name at execution time.
    """

    step_label: str
    """High-level step name for the audit-log description, e.g.
    'rewire-qlocal-as-qremote', 'define-bridge-sdr'."""

    target_qm_pod_for: str
    """'source' or 'target' — which side the MQSC executes against."""

    op_kind: AuditOperation
    object_kind: str
    object_name: str
    mqsc_text: str
    rationale: str
    rollback_text: str | None
    rollback_op_kind: AuditOperation | None
    """The audit op that should be recorded for rollback execution.
    None when the command has no inverse (e.g. ALTER QMGR with no
    captured prior state)."""

    related_flow_indices: tuple[int, ...] = ()


# ─────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────


def _q(name: str) -> str:
    """Single-quote an MQ name for MQSC."""
    return f"'{name}'"


# ─────────────────────────────────────────────────────────────────────────
# Pre-rewire probes — DISPLAY commands only
# ─────────────────────────────────────────────────────────────────────────


def build_pre_validation_probes(
    *,
    source_qm: str,
    app_source_queues: Iterable[str],
) -> list[MigrationMqscCommand]:
    """Build the DISPLAY-only probes for VALIDATING_PRE.

    Captures baseline CURDEPTH / IPPROCS / OPPROCS / CHSTATE for each
    queue the app owns on the source QM. Read-only; no rollback.
    """
    cmds: list[MigrationMqscCommand] = []
    for qname in sorted(set(app_source_queues)):
        cmds.append(
            MigrationMqscCommand(
                step_label="probe-source-qlocal",
                target_qm_pod_for="source",
                op_kind=AuditOperation.MQSC_ALTER_QMGR,  # umbrella for DISPLAY
                object_kind="QLOCAL",
                object_name=qname,
                mqsc_text=(
                    f"DISPLAY QLOCAL({_q(qname)}) "
                    "CURDEPTH IPPROCS OPPROCS"
                ),
                rationale=(
                    f"Capture baseline depth + reader/writer counts for "
                    f"{qname} on {source_qm} before rewiring."
                ),
                rollback_text=None,
                rollback_op_kind=None,
            )
        )
    return cmds


# ─────────────────────────────────────────────────────────────────────────
# REWIRING — the substantive forward action
# ─────────────────────────────────────────────────────────────────────────


def build_rewire_plan(
    *,
    app_id: str,
    source_qm: str,
    target_qm: str,
    target_qm_namespace: str,
    target_qm_listener_port: int,
    flows: Iterable[FlowSpec],
) -> list[MigrationMqscCommand]:
    """Build the REWIRING plan for app_id from source_qm to target_qm.

    Walks the target topology's flows. For every flow where this app is
    producer or consumer, emits the source-side rewiring commands that
    redirect traffic through the source -> target bridge.

    The target side does NOT need new application queues — those were
    created by the target topology's realize-mq-objects run. We only
    need a RCVR channel on the target so the bridge SDR has somewhere
    to terminate.

    Returns commands in deterministic order. The engine assigns
    step_index = position in this list.
    """
    flows_list = list(flows)

    # Build the per-app set of queue names that need to "move" off
    # source. These are queues where this app is the consumer (its
    # consumer_queue_name on the target QM is where it now receives
    # messages) — the same name must be a QREMOTE on the source so
    # producers still routing to source see their messages forwarded.
    #
    # In the source topology, this app's consumer queues are QLOCALs
    # on whatever shared source QM hosted it; in the target topology,
    # they're QLOCALs on the app's dedicated target QM. The migration
    # bridges that gap.

    queues_to_redirect: dict[str, list[int]] = {}
    """consumer_queue_name -> list of flow indices that motivate it."""

    bridge_needed_for_outbound: bool = False
    """True if this app is a producer in any flow — we will need to
    forward its produced traffic to the new target via the bridge
    SDR/XMITQ."""

    for idx, flow in enumerate(flows_list):
        if flow.consumer_app_id == app_id:
            queues_to_redirect.setdefault(flow.consumer_queue_name, []).append(idx)
        if flow.producer_app_id == app_id:
            bridge_needed_for_outbound = True

    commands: list[MigrationMqscCommand] = []

    # ── 1. Bridge XMITQ on source ────────────────────────────────────
    bridge_xmitq = bridge_xmitq_name(target_qm)
    bridge_channel = bridge_channel_name(source_qm, target_qm)
    bridge_conname = _service_conname(
        target_qm,
        namespace=target_qm_namespace,
        listener_port=target_qm_listener_port,
    )

    commands.append(
        MigrationMqscCommand(
            step_label="define-bridge-xmitq",
            target_qm_pod_for="source",
            op_kind=AuditOperation.MQSC_DEFINE_QXMIT,
            object_kind="QXMIT",
            object_name=bridge_xmitq,
            mqsc_text=(
                f"DEFINE QLOCAL({_q(bridge_xmitq)}) USAGE(XMITQ) "
                "TRIGGER TRIGTYPE(FIRST) "
                "INITQ('SYSTEM.CHANNEL.INITQ') "
                f"TRIGDATA({_q(bridge_channel)}) REPLACE"
            ),
            rationale=(
                f"Transmission queue on {source_qm} staging messages "
                f"for the bridge channel that forwards to {target_qm}."
            ),
            rollback_text=f"DELETE QLOCAL({_q(bridge_xmitq)})",
            rollback_op_kind=AuditOperation.MQSC_DELETE_QXMIT,
        )
    )

    # ── 2. Bridge RCVR on target (so SDR can land) ───────────────────
    commands.append(
        MigrationMqscCommand(
            step_label="define-bridge-rcvr",
            target_qm_pod_for="target",
            op_kind=AuditOperation.MQSC_DEFINE_CHANNEL_RCVR,
            object_kind="CHANNEL_RCVR",
            object_name=bridge_channel,
            mqsc_text=(
                f"DEFINE CHANNEL({_q(bridge_channel)}) "
                "CHLTYPE(RCVR) TRPTYPE(TCP) REPLACE"
            ),
            rationale=(
                f"Receiver channel on {target_qm} terminating the "
                f"bridge from {source_qm}. Required so the source-side "
                "SDR has a peer to connect to."
            ),
            rollback_text=f"DELETE CHANNEL({_q(bridge_channel)})",
            rollback_op_kind=AuditOperation.MQSC_DELETE_CHANNEL,
        )
    )

    # ── 3. Bridge SDR on source ──────────────────────────────────────
    commands.append(
        MigrationMqscCommand(
            step_label="define-bridge-sdr",
            target_qm_pod_for="source",
            op_kind=AuditOperation.MQSC_DEFINE_CHANNEL_SDR,
            object_kind="CHANNEL_SDR",
            object_name=bridge_channel,
            mqsc_text=(
                f"DEFINE CHANNEL({_q(bridge_channel)}) "
                "CHLTYPE(SDR) TRPTYPE(TCP) "
                f"XMITQ({_q(bridge_xmitq)}) "
                f"CONNAME('{bridge_conname}') REPLACE"
            ),
            rationale=(
                f"Sender channel on {source_qm} that reads from "
                f"{bridge_xmitq} and transmits to {target_qm} at "
                f"{bridge_conname}."
            ),
            rollback_text=f"DELETE CHANNEL({_q(bridge_channel)})",
            rollback_op_kind=AuditOperation.MQSC_DELETE_CHANNEL,
        )
    )

    # ── 4. START the bridge SDR ──────────────────────────────────────
    commands.append(
        MigrationMqscCommand(
            step_label="start-bridge-sdr",
            target_qm_pod_for="source",
            op_kind=AuditOperation.MQSC_START_CHANNEL,
            object_kind="CHANNEL_SDR",
            object_name=bridge_channel,
            mqsc_text=f"START CHANNEL({_q(bridge_channel)})",
            rationale=(
                "Move the bridge SDR from INACTIVE to RUNNING so "
                "messages on the XMITQ are transmitted immediately. "
                "Without this, messages would pile up on the XMITQ "
                "until the channel initiator fires the trigger."
            ),
            rollback_text=f"STOP CHANNEL({_q(bridge_channel)})",
            rollback_op_kind=AuditOperation.MQSC_STOP_CHANNEL,
        )
    )

    # ── 5. For each consumer-side queue this app owns, swap QLOCAL
    #      for QREMOTE pointing at the target ─────────────────────────
    for qname in sorted(queues_to_redirect):
        flow_idx = tuple(sorted(queues_to_redirect[qname]))

        # First DELETE the QLOCAL. The original QLOCAL had a depth at
        # rewire time; we explicitly DRAIN-WAIT before deletion via the
        # state machine, so by the time this command runs depth=0
        # (zero-window verified). If a producer race-condition adds a
        # message between drain-end and DELETE-QLOCAL, that message is
        # lost — this is a known edge case the engine handles by an
        # additional dry-run depth probe immediately before this DELETE.

        commands.append(
            MigrationMqscCommand(
                step_label="delete-source-qlocal",
                target_qm_pod_for="source",
                op_kind=AuditOperation.MQSC_DELETE_QLOCAL,
                object_kind="QLOCAL",
                object_name=qname,
                mqsc_text=f"DELETE QLOCAL({_q(qname)})",
                rationale=(
                    f"Remove the source-side QLOCAL {qname} now that "
                    f"the app is hosted on {target_qm}. Drain has "
                    "been verified zero-window per Little's Law before "
                    "this point."
                ),
                # Rollback recreates the QLOCAL (REPLACE handles the
                # case where the rollback re-runs after partial recovery).
                rollback_text=f"DEFINE QLOCAL({_q(qname)}) REPLACE",
                rollback_op_kind=AuditOperation.MQSC_DEFINE_QLOCAL,
                related_flow_indices=flow_idx,
            )
        )

        # Then DEFINE the QREMOTE in its place. RNAME and RQMNAME point
        # at the same queue name on the target QM (where realize-mq-
        # objects already created the QLOCAL).
        commands.append(
            MigrationMqscCommand(
                step_label="define-source-qremote",
                target_qm_pod_for="source",
                op_kind=AuditOperation.MQSC_DEFINE_QREMOTE,
                object_kind="QREMOTE",
                object_name=qname,
                mqsc_text=(
                    f"DEFINE QREMOTE({_q(qname)}) "
                    f"RNAME({_q(qname)}) "
                    f"RQMNAME({_q(target_qm)}) "
                    f"XMITQ({_q(bridge_xmitq)}) REPLACE"
                ),
                rationale=(
                    f"Producers still routing to {source_qm}.{qname} "
                    f"now have their messages forwarded to "
                    f"{target_qm}.{qname} via {bridge_xmitq}. "
                    "Transparent rewiring: no producer/consumer reconfig."
                ),
                rollback_text=f"DELETE QREMOTE({_q(qname)})",
                rollback_op_kind=AuditOperation.MQSC_DELETE_QREMOTE,
                related_flow_indices=flow_idx,
            )
        )

    return commands


def _service_conname(
    target_qm: str, *, namespace: str, listener_port: int
) -> str:
    """Compute the in-cluster DNS CONNAME for a target QM.

    Mirrors the rule in `mqsc_derivation.compute_conname`. Replicated
    here to keep `migration.choreography` pure (no transitive
    `provisioning` import). The two implementations must stay in sync;
    the test suite asserts equivalence on representative QM names.
    """
    import re
    lowered = target_qm.lower()
    safe = re.sub(r"[^a-z0-9-]", "-", lowered)
    safe = re.sub(r"-+", "-", safe).strip("-")
    if not safe:
        safe = "qm"
    return f"qm-{safe}.{namespace}.svc.cluster.local({listener_port})"


# ─────────────────────────────────────────────────────────────────────────
# App-flow helpers — used by engine + planner
# ─────────────────────────────────────────────────────────────────────────


def app_owns_queues_on_source(
    *,
    app_id: str,
    source_topology_flows: Iterable[FlowSpec],
) -> dict[str, list[int]]:
    """Return the queues this app currently owns on the source side.

    A queue "belongs" to the app if either:
      - it is the app's consumer_queue (the app is the consumer)
      - it is the app's producer_queue AND that queue is a QLOCAL
        on a Local flow (the app produces and consumes in the same
        place; the queue lives on the app's home QM)

    Returns: queue_name -> list of flow indices in the source spec.
    """
    flows = list(source_topology_flows)
    out: dict[str, list[int]] = {}
    for idx, flow in enumerate(flows):
        if flow.consumer_app_id == app_id:
            out.setdefault(flow.consumer_queue_name, []).append(idx)
        if (
            flow.producer_app_id == app_id
            and flow.flow_type == "Local"
            and flow.producer_queue_name == flow.consumer_queue_name
        ):
            out.setdefault(flow.producer_queue_name, []).append(idx)
    return out


def app_source_qm(
    *,
    app_id: str,
    source_topology_flows: Iterable[FlowSpec],
) -> str | None:
    """Return the source QM that hosts this app.

    Strict 1:1 invariant per Phase 2 brief: every app lives on
    exactly one source QM. If the topology somehow violates this,
    return the first observed QM and let the engine flag the
    inconsistency via a warning.
    """
    flows = list(source_topology_flows)
    candidates: list[str] = []
    for flow in flows:
        if flow.consumer_app_id == app_id:
            candidates.append(flow.consumer_queue_manager)
        if flow.producer_app_id == app_id:
            candidates.append(flow.producer_queue_manager)
    if not candidates:
        return None
    return candidates[0]


def app_target_qm(
    *,
    app_id: str,
    target_topology_flows: Iterable[FlowSpec],
) -> str | None:
    """Return the target QM that should host this app post-migration."""
    flows = list(target_topology_flows)
    for flow in flows:
        if flow.consumer_app_id == app_id:
            return flow.consumer_queue_manager
        if flow.producer_app_id == app_id:
            return flow.producer_queue_manager
    return None


__all__ = [
    "MigrationMqscCommand",
    "bridge_channel_name",
    "bridge_xmitq_name",
    "build_pre_validation_probes",
    "build_rewire_plan",
    "app_owns_queues_on_source",
    "app_source_qm",
    "app_target_qm",
]
