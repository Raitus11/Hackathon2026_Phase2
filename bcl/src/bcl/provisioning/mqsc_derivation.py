"""Pure MQSC command derivation from a topology's flow spec.

This module is intentionally I/O-free:
  - No subprocess calls
  - No database access
  - No HTTP, no Kubernetes
  - No global state

Given a queue-manager name and the topology's flows, it produces a
deterministic, idempotent MQSC plan to realize all the MQ objects that
QM needs to own for those flows: local queues, remote queues, transmission
queues, sender channels, and receiver channels.

Determinism matters because:
  1. The same input always produces the same audit-logged MQSC text.
     Operators can reproduce a run command-for-command.
  2. We can unit-test against the real CSV without spinning up MQ.
  3. The rollback engine (later) can re-derive what objects exist by
     re-running this function — no need to query the live QM.

Idempotency is handled here at the command-text level (`REPLACE` clauses
on the DEFINE commands), and at the call level by `mq_realize.py`
tolerating `AMQ8350` / `AMQ8013` already-exists codes.

Why not skip REPLACE and rely solely on AMQ-code tolerance?
Both layers matter. REPLACE handles attribute drift (if a queue exists
but with different attributes, REPLACE harmonizes it). AMQ-code tolerance
handles raw-create attempts that didn't use REPLACE (e.g. operator-driven
MQSC dry-runs).

MQSC reference: IBM MQ 9.4 Reference,
  https://www.ibm.com/docs/en/ibm-mq/9.4?topic=mqsc-commands
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from bcl.models.api import FlowSpec
from bcl.models.orm import AuditOperation


# ─────────────────────────────────────────────────────────────────────────
# Data shapes
# ─────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class MqscCommand:
    """One MQSC command to apply, with all the metadata needed to
    audit-log it and roll it back later.

    Fields are intentionally minimal — anything richer (queue depth,
    last-modified, etc.) belongs to live runtime state, not the plan.
    """

    op_kind: AuditOperation
    """Which audit op this command corresponds to. Maps 1:1 to
    AuditOperation.MQSC_* values, enabling rollback inversion."""

    object_kind: str
    """High-level object kind: 'QLOCAL', 'QREMOTE', 'QXMIT',
    'CHANNEL_SDR', 'CHANNEL_RCVR', 'QMGR'."""

    object_name: str
    """The MQ-name of the object being defined. For QMGR-level commands
    (ALTER QMGR), this is the queue-manager name itself."""

    mqsc_text: str
    """The literal MQSC command to send to runmqsc, without trailing newline."""

    rationale: str
    """One-sentence why this command exists. Surfaces in audit log
    so reviewers don't have to deduce it from the MQSC text."""

    rollback_text: str | None = None
    """Inverse command for the rollback engine. None = command is not
    individually rollable (e.g. ALTER QMGR with no captured prior state)."""

    related_flows: tuple[int, ...] = ()
    """Indices into the input flow list that motivated this command.
    Used for traceability — "why does QM X have queue Y?" links back
    to the source CSV rows that required it."""


@dataclass(frozen=True)
class MqscPlan:
    """The full MQSC plan for one queue manager."""

    qm_name: str
    commands: tuple[MqscCommand, ...]

    # Bookkeeping that's cheaper to compute here than at every consumer.
    local_queues: tuple[str, ...] = field(default_factory=tuple)
    remote_queues: tuple[str, ...] = field(default_factory=tuple)
    transmit_queues: tuple[str, ...] = field(default_factory=tuple)
    sender_channels: tuple[str, ...] = field(default_factory=tuple)
    receiver_channels: tuple[str, ...] = field(default_factory=tuple)

    warnings: tuple[str, ...] = field(default_factory=tuple)
    """Non-fatal cross-flow inconsistencies surfaced during derivation.

    Examples: a channel name shared by flows with different XMITQs (only
    one XMITQ binding wins); a QREMOTE name with conflicting RNAME/RQMNAME.
    These usually reflect production CSV oddities and are surfaced to the
    operator rather than crashing the plan.
    """

    def as_mqsc_batch(self) -> str:
        """Concatenate all commands into one runmqsc-ready text blob.

        We send one command at a time in the executor so that a single
        failure doesn't poison the rest of the batch — but this helper is
        useful for dry-run preview and for auditing the full plan as one
        document.
        """
        return "\n".join(c.mqsc_text for c in self.commands) + "\n"

    def to_summary_dict(self) -> dict[str, Any]:
        """Plain-dict summary for API responses / audit payloads."""
        return {
            "qm_name": self.qm_name,
            "command_count": len(self.commands),
            "local_queues": list(self.local_queues),
            "remote_queues": list(self.remote_queues),
            "transmit_queues": list(self.transmit_queues),
            "sender_channels": list(self.sender_channels),
            "receiver_channels": list(self.receiver_channels),
            "warnings": list(self.warnings),
        }


# ─────────────────────────────────────────────────────────────────────────
# CONNAME computation (pure)
# ─────────────────────────────────────────────────────────────────────────


def compute_conname(
    consumer_qm_name: str,
    *,
    namespace: str,
    listener_port: int,
) -> str:
    """K8s in-cluster DNS for the consumer QM's Service.

    Service name is derived the same way the provisioning engine derives
    it from the QM name. Importing the naming module would tightly couple
    derivation to the K8s layer; instead we replicate the rule here
    (lowercased, non-alnum -> '-', prefix 'qm-'). This keeps derivation
    pure — no transitive imports — and it's documented as a contract:
    "this function must match naming.service_name() for the same input."

    See bcl/provisioning/naming.py service_name() for the canonical impl.
    """
    # Conservative replication of naming.k8s_safe rule for our QM-name
    # alphabet (uppercase letters, digits, underscores, dots, slashes).
    import re

    lowered = consumer_qm_name.lower()
    safe = re.sub(r"[^a-z0-9-]", "-", lowered)
    safe = re.sub(r"-+", "-", safe).strip("-")
    if not safe:
        # Pathological; shouldn't happen for validated MQ names.
        safe = "qm"
    service = f"qm-{safe}"
    return f"{service}.{namespace}.svc.cluster.local({listener_port})"


# ─────────────────────────────────────────────────────────────────────────
# Quoting helper
# ─────────────────────────────────────────────────────────────────────────


def _mq_quote(name: str) -> str:
    """Wrap an MQ name in single quotes for MQSC.

    MQ names that contain dots, slashes, or are exactly 48 chars are
    safest when quoted. We quote all names unconditionally — costs
    nothing, and runmqsc accepts quoted names everywhere unquoted ones
    are valid.

    Single-quotes inside MQ names are illegal (per IBM MQ naming rules
    enforced by api._validate_mq_name), so no escaping needed.
    """
    return f"'{name}'"


# ─────────────────────────────────────────────────────────────────────────
# Command builders — each one is a pure function returning one MqscCommand
# ─────────────────────────────────────────────────────────────────────────


def _build_alter_qmgr_deadq(qm_name: str, dlq_name: str) -> MqscCommand:
    """Point the QM's DEADQ attribute at our standard DLQ name.

    Brief constraint #3: every QM has a DLQ. Standard enterprise MQ practice;
    treated as non-negotiable here. The DLQ itself is also defined as a local queue
    (see _build_dlq_qlocal).
    """
    return MqscCommand(
        op_kind=AuditOperation.MQSC_ALTER_QMGR,
        object_kind="QMGR",
        object_name=qm_name,
        mqsc_text=f"ALTER QMGR DEADQ({dlq_name})",
        rationale=(
            f"Set the queue-manager-wide dead-letter queue to {dlq_name}. "
            "Required by brief constraint #3: every QM has a DLQ."
        ),
        rollback_text=None,  # We don't capture prior DEADQ; intentionally not rollable.
        related_flows=(),
    )


def _build_dlq_qlocal(dlq_name: str) -> MqscCommand:
    """Define the DLQ itself as a local queue."""
    return MqscCommand(
        op_kind=AuditOperation.MQSC_DEFINE_QLOCAL,
        object_kind="QLOCAL",
        object_name=dlq_name,
        mqsc_text=f"DEFINE QLOCAL({_mq_quote(dlq_name)}) REPLACE",
        rationale="Backing local queue for the QM-wide dead-letter queue.",
        rollback_text=f"DELETE QLOCAL({_mq_quote(dlq_name)})",
        related_flows=(),
    )


def _build_qlocal(queue_name: str, *, flow_indices: tuple[int, ...]) -> MqscCommand:
    """Define a local queue. Used for both local-flow queues and
    consumer-side queues that messages land in after channel transmission.
    """
    return MqscCommand(
        op_kind=AuditOperation.MQSC_DEFINE_QLOCAL,
        object_kind="QLOCAL",
        object_name=queue_name,
        mqsc_text=f"DEFINE QLOCAL({_mq_quote(queue_name)}) REPLACE",
        rationale=(
            "Local queue holding messages until consumed. "
            f"Required by source CSV flow(s) {list(flow_indices)}."
        ),
        rollback_text=f"DELETE QLOCAL({_mq_quote(queue_name)})",
        related_flows=flow_indices,
    )


def _build_qxmit(xmitq_name: str, *, flow_indices: tuple[int, ...]) -> MqscCommand:
    """Define a transmission queue (QLOCAL with USAGE(XMITQ)).

    The XMITQ is the producer-side staging queue. A sender channel reads
    from it; the channel transmits messages over TCP to the receiver
    channel on the consumer side, which puts them on the destination
    local queue. Reference: IBM MQ docs, "Transmission queues",
    https://www.ibm.com/docs/en/ibm-mq/9.4?topic=queues-transmission
    """
    return MqscCommand(
        op_kind=AuditOperation.MQSC_DEFINE_QXMIT,
        object_kind="QXMIT",
        object_name=xmitq_name,
        mqsc_text=(
            f"DEFINE QLOCAL({_mq_quote(xmitq_name)}) USAGE(XMITQ) REPLACE"
        ),
        rationale=(
            "Transmission queue staging messages for a sender channel. "
            f"Required by source CSV flow(s) {list(flow_indices)}."
        ),
        rollback_text=f"DELETE QLOCAL({_mq_quote(xmitq_name)})",
        related_flows=flow_indices,
    )


def _build_qremote(
    qremote_name: str,
    *,
    rname: str,
    rqmname: str,
    xmitq: str,
    flow_indices: tuple[int, ...],
) -> MqscCommand:
    """Define a remote queue definition (QREMOTE).

    A QREMOTE is the producer-side handle that producers PUT to. MQ
    rewrites the destination at PUT time using the QREMOTE's RNAME
    (the real queue name on the remote QM) and RQMNAME (the remote
    QM name), placing the message on the named XMITQ for the sender
    channel to pick up. Reference: IBM MQ docs, "Remote queues",
    https://www.ibm.com/docs/en/ibm-mq/9.4?topic=qm-remote-queue-objects
    """
    return MqscCommand(
        op_kind=AuditOperation.MQSC_DEFINE_QREMOTE,
        object_kind="QREMOTE",
        object_name=qremote_name,
        mqsc_text=(
            f"DEFINE QREMOTE({_mq_quote(qremote_name)}) "
            f"RNAME({_mq_quote(rname)}) "
            f"RQMNAME({_mq_quote(rqmname)}) "
            f"XMITQ({_mq_quote(xmitq)}) "
            "REPLACE"
        ),
        rationale=(
            f"Remote queue handle resolving to {rname} on {rqmname} via {xmitq}. "
            f"Required by source CSV flow(s) {list(flow_indices)}."
        ),
        rollback_text=f"DELETE QREMOTE({_mq_quote(qremote_name)})",
        related_flows=flow_indices,
    )


def _build_channel_sdr(
    channel_name: str,
    *,
    xmitq: str,
    conname: str,
    flow_indices: tuple[int, ...],
) -> MqscCommand:
    """Define a sender (SDR) channel.

    The SDR reads from XMITQ and transmits to a matching RCVR channel
    (same name) on the consumer QM. CONNAME is host(port). MQ docs:
    https://www.ibm.com/docs/en/ibm-mq/9.4?topic=channels-types
    """
    return MqscCommand(
        op_kind=AuditOperation.MQSC_DEFINE_CHANNEL_SDR,
        object_kind="CHANNEL_SDR",
        object_name=channel_name,
        mqsc_text=(
            f"DEFINE CHANNEL({_mq_quote(channel_name)}) "
            "CHLTYPE(SDR) "
            "TRPTYPE(TCP) "
            f"XMITQ({_mq_quote(xmitq)}) "
            f"CONNAME('{conname}') "
            "REPLACE"
        ),
        rationale=(
            f"Sender channel from this QM to {conname}, reading from {xmitq}. "
            f"Required by source CSV flow(s) {list(flow_indices)}."
        ),
        rollback_text=f"DELETE CHANNEL({_mq_quote(channel_name)})",
        related_flows=flow_indices,
    )


def _build_channel_rcvr(
    channel_name: str,
    *,
    flow_indices: tuple[int, ...],
) -> MqscCommand:
    """Define a receiver (RCVR) channel.

    RCVR is the passive endpoint of the channel pair; it has no CONNAME.
    The remote SDR connects to this QM's listener (port 1414) and
    addresses this channel by name. MQ docs:
    https://www.ibm.com/docs/en/ibm-mq/9.4?topic=channels-types
    """
    return MqscCommand(
        op_kind=AuditOperation.MQSC_DEFINE_CHANNEL_RCVR,
        object_kind="CHANNEL_RCVR",
        object_name=channel_name,
        mqsc_text=(
            f"DEFINE CHANNEL({_mq_quote(channel_name)}) "
            "CHLTYPE(RCVR) "
            "TRPTYPE(TCP) "
            "REPLACE"
        ),
        rationale=(
            "Receiver channel terminating a sender on the peer QM. "
            f"Required by source CSV flow(s) {list(flow_indices)}."
        ),
        rollback_text=f"DELETE CHANNEL({_mq_quote(channel_name)})",
        related_flows=flow_indices,
    )


# ─────────────────────────────────────────────────────────────────────────
# Main entry: derive the full plan for one QM from a list of flows
# ─────────────────────────────────────────────────────────────────────────


def derive_mqsc_for_qm(
    *,
    qm_name: str,
    flows: Iterable[FlowSpec],
    namespace: str,
    listener_port: int,
    dlq_name: str = "SYSTEM.DEAD.LETTER.QUEUE",
) -> MqscPlan:
    """Compute the MQSC plan for `qm_name` given the topology's flows.

    Walks every flow once. For each flow, if this QM is involved
    (as producer-side, consumer-side, or both for Local flows), emits
    the MQSC commands needed.

    Deduplication: the same (object_kind, object_name) is emitted at most
    once. MQ objects are shared across flows — the same XMITQ serves many
    QREMOTEs to the same destination QM; the same SDR channel transmits
    messages for many queues. We track what we've emitted in a set keyed
    on (kind, name).

    Args:
        qm_name: the queue manager this plan is for (must match flow QMs).
        flows: the topology's flow list.
        namespace: K8s namespace where target consumer QMs are deployed
            (used to compute SDR CONNAME via service DNS).
        listener_port: MQ listener port on consumer QM pods (typically 1414).
        dlq_name: DLQ name (default IBM MQ system DLQ).

    Returns:
        MqscPlan with deterministic command ordering:
            1. ALTER QMGR DEADQ
            2. DEFINE QLOCAL for DLQ (idempotent — won't fail if pre-existing)
            3. DEFINE QLOCAL for each local-flow queue, alphabetically by name
            4. DEFINE QLOCAL for each consumer-side queue on remote flows
            5. DEFINE QLOCAL(...) USAGE(XMITQ) for each XMITQ
            6. DEFINE QREMOTE for each remote-flow producer-side queue
            7. DEFINE CHANNEL(...) CHLTYPE(SDR) for each unique outbound channel
            8. DEFINE CHANNEL(...) CHLTYPE(RCVR) for each unique inbound channel

    Ordering rationale: queue objects before channels (channels reference
    XMITQs by name; XMITQs must exist first). Within a category,
    alphabetical ordering for deterministic audit logs.
    """
    flows_list = list(flows)

    # Cross-flow inconsistencies, surfaced rather than raised.
    warnings: list[str] = []

    # Per-kind dedup sets, also accumulating which flow indices motivated each.
    # Key = object_name; Value = sorted tuple of flow indices.
    local_queues: dict[str, list[int]] = {}
    consumer_side_locals: dict[str, list[int]] = {}
    transmit_queues: dict[str, list[int]] = {}
    # QREMOTE is keyed by (producer_queue_name) since one QREMOTE per
    # producer-side queue handle.
    remote_queues: dict[
        str, tuple[str, str, str, list[int]]
    ] = {}  # qremote_name -> (rname, rqmname, xmitq, flow_indices)
    # Channels: keyed by channel name. Same channel name can be motivated
    # by many flows. We capture (xmitq, consumer_qm) for SDR; nothing extra
    # for RCVR.
    sender_channels: dict[str, tuple[str, str, list[int]]] = {}
    # sdr_name -> (xmitq, consumer_qm, flow_indices)
    receiver_channels: dict[str, list[int]] = {}

    for idx, flow in enumerate(flows_list):
        is_local = flow.flow_type == "Local"
        is_producer = flow.producer_queue_manager == qm_name
        is_consumer = flow.consumer_queue_manager == qm_name

        if not (is_producer or is_consumer):
            continue

        if is_local:
            # Local flow: producer_qm == consumer_qm == this QM.
            # Best case: producer_queue_name == consumer_queue_name, one
            # QLOCAL serves both put and get. Production CSVs sometimes
            # report mismatched queue names on a same-QM flow — this is
            # not strict MQ semantics (a "local flow" with two different
            # queues isn't actually local in MQ terms) but we tolerate it
            # by defining both queues and warning.
            if flow.producer_queue_name == flow.consumer_queue_name:
                local_queues.setdefault(flow.producer_queue_name, []).append(idx)
            else:
                warnings.append(
                    f"Local flow {idx} on {qm_name} has different "
                    f"producer queue ({flow.producer_queue_name!r}) and "
                    f"consumer queue ({flow.consumer_queue_name!r}). "
                    "Defining both as local queues; delivery between "
                    "them is not configured by this plan and would need "
                    "an MQ alias, message broker rule, or app-level bridge."
                )
                local_queues.setdefault(flow.producer_queue_name, []).append(idx)
                local_queues.setdefault(flow.consumer_queue_name, []).append(idx)
            continue

        # Remote flow.
        if is_producer:
            # Producer side: QREMOTE handle, XMITQ, SDR channel.
            if not (flow.transmit_queue_name and flow.channel_name):
                # FlowSpec validator should have caught this, defense in depth.
                raise ValueError(
                    f"Remote flow {idx} missing transmit_queue_name or channel_name"
                )
            # QREMOTE: name = producer_queue_name, RNAME = consumer_queue_name,
            # RQMNAME = consumer_queue_manager, XMITQ = transmit_queue_name.
            qremote_name = flow.producer_queue_name
            existing = remote_queues.get(qremote_name)
            if existing is None:
                remote_queues[qremote_name] = (
                    flow.consumer_queue_name,
                    flow.consumer_queue_manager,
                    flow.transmit_queue_name,
                    [idx],
                )
            else:
                rname, rqmname, xmitq, fi = existing
                # Same QREMOTE name with different destinations is a real
                # production-CSV pattern observed in operational data —
                # don't crash, record a warning and keep the first-seen
                # binding. The derivation must be total over all valid
                # FlowSpec inputs.
                if (rname, rqmname, xmitq) != (
                    flow.consumer_queue_name,
                    flow.consumer_queue_manager,
                    flow.transmit_queue_name,
                ):
                    warnings.append(
                        f"QREMOTE {qremote_name!r} on {qm_name} has "
                        f"inconsistent definitions across flows: flow "
                        f"{fi[0]} -> ({rname},{rqmname},{xmitq}); flow "
                        f"{idx} -> ({flow.consumer_queue_name},"
                        f"{flow.consumer_queue_manager},"
                        f"{flow.transmit_queue_name}). "
                        "Keeping first-seen binding."
                    )
                fi.append(idx)

            transmit_queues.setdefault(flow.transmit_queue_name, []).append(idx)

            # SDR channel keyed by name.
            sdr = sender_channels.get(flow.channel_name)
            if sdr is None:
                sender_channels[flow.channel_name] = (
                    flow.transmit_queue_name,
                    flow.consumer_queue_manager,
                    [idx],
                )
            else:
                xmitq, peer_qm, fi = sdr
                if (xmitq, peer_qm) != (
                    flow.transmit_queue_name,
                    flow.consumer_queue_manager,
                ):
                    # Channel name reused across flows with different
                    # XMITQs is the most common malformedness in real
                    # production exports. A channel reads from exactly
                    # one XMITQ. Whichever XMITQ we chose first remains
                    # the channel's binding; messages routed through the
                    # *other* XMITQ will accumulate without delivery.
                    # Surface this so operators can repair the topology.
                    warnings.append(
                        f"SDR channel {flow.channel_name!r} on {qm_name} "
                        f"is referenced by flow {fi[0]} with XMITQ {xmitq} "
                        f"and by flow {idx} with XMITQ {flow.transmit_queue_name}. "
                        "A channel reads from exactly one XMITQ; keeping "
                        f"first-seen binding ({xmitq}). Messages routed "
                        f"through {flow.transmit_queue_name} will not be "
                        "transmitted by this channel."
                    )
                fi.append(idx)

        if is_consumer:
            # Consumer side: QLOCAL for the destination, RCVR channel.
            consumer_side_locals.setdefault(flow.consumer_queue_name, []).append(idx)
            if flow.channel_name:
                receiver_channels.setdefault(flow.channel_name, []).append(idx)

    # ── Compose commands in deterministic order ──────────────────────

    commands: list[MqscCommand] = []

    # 1. ALTER QMGR
    commands.append(_build_alter_qmgr_deadq(qm_name, dlq_name))

    # 2. DLQ itself
    commands.append(_build_dlq_qlocal(dlq_name))

    # 3. Local-flow queues, sorted by name
    for qname in sorted(local_queues):
        commands.append(
            _build_qlocal(qname, flow_indices=tuple(sorted(local_queues[qname])))
        )

    # 4. Consumer-side QLOCALs for remote flows, sorted, excluding any
    #    already covered by local_queues (a queue can't be both — different
    #    flow_type — but be defensive).
    for qname in sorted(set(consumer_side_locals) - set(local_queues)):
        commands.append(
            _build_qlocal(
                qname, flow_indices=tuple(sorted(consumer_side_locals[qname]))
            )
        )

    # 5. XMITQs, sorted
    for xname in sorted(transmit_queues):
        commands.append(
            _build_qxmit(xname, flow_indices=tuple(sorted(transmit_queues[xname])))
        )

    # 6. QREMOTEs, sorted by name
    for qrname in sorted(remote_queues):
        rname, rqmname, xmitq, fi = remote_queues[qrname]
        commands.append(
            _build_qremote(
                qrname,
                rname=rname,
                rqmname=rqmname,
                xmitq=xmitq,
                flow_indices=tuple(sorted(fi)),
            )
        )

    # 7. SDR channels, sorted by name
    for cname in sorted(sender_channels):
        xmitq, peer_qm, fi = sender_channels[cname]
        conname = compute_conname(
            peer_qm, namespace=namespace, listener_port=listener_port
        )
        commands.append(
            _build_channel_sdr(
                cname,
                xmitq=xmitq,
                conname=conname,
                flow_indices=tuple(sorted(fi)),
            )
        )

    # 8. RCVR channels, sorted by name
    for cname in sorted(receiver_channels):
        commands.append(
            _build_channel_rcvr(
                cname, flow_indices=tuple(sorted(receiver_channels[cname]))
            )
        )

    return MqscPlan(
        qm_name=qm_name,
        commands=tuple(commands),
        local_queues=tuple(sorted(set(local_queues) | set(consumer_side_locals))),
        remote_queues=tuple(sorted(remote_queues)),
        transmit_queues=tuple(sorted(transmit_queues)),
        sender_channels=tuple(sorted(sender_channels)),
        receiver_channels=tuple(sorted(receiver_channels)),
        warnings=tuple(warnings),
    )


def derive_mqsc_for_topology(
    *,
    flows: Iterable[FlowSpec],
    namespace: str,
    listener_port: int,
    dlq_name: str = "SYSTEM.DEAD.LETTER.QUEUE",
) -> dict[str, MqscPlan]:
    """Derive plans for every distinct QM appearing in the flows.

    Convenience wrapper around `derive_mqsc_for_qm`. Returns a dict keyed
    by QM name, alphabetically ordered (dict insertion order is preserved
    in Python 3.7+).
    """
    flows_list = list(flows)
    qm_names: set[str] = set()
    for f in flows_list:
        qm_names.add(f.producer_queue_manager)
        qm_names.add(f.consumer_queue_manager)

    return {
        qm: derive_mqsc_for_qm(
            qm_name=qm,
            flows=flows_list,
            namespace=namespace,
            listener_port=listener_port,
            dlq_name=dlq_name,
        )
        for qm in sorted(qm_names)
    }


# ─────────────────────────────────────────────────────────────────────────
# Inverse plan derivation — for teardown / rollback
# ─────────────────────────────────────────────────────────────────────────


# Map each forward audit op to its inverse op for teardown.
# These all exist in AuditOperation (see orm.py). We never invert
# ALTER QMGR DEADQ — there's no captured prior DEADQ to restore to,
# so the inverse is "no-op" and we filter those commands out of the
# teardown plan rather than fabricate a fake DELETE.
_INVERSE_AUDIT_OP: dict[AuditOperation, AuditOperation] = {
    AuditOperation.MQSC_DEFINE_QLOCAL: AuditOperation.MQSC_DELETE_QLOCAL,
    AuditOperation.MQSC_DEFINE_QREMOTE: AuditOperation.MQSC_DELETE_QREMOTE,
    AuditOperation.MQSC_DEFINE_QXMIT: AuditOperation.MQSC_DELETE_QXMIT,
    AuditOperation.MQSC_DEFINE_CHANNEL_SDR: AuditOperation.MQSC_DELETE_CHANNEL,
    AuditOperation.MQSC_DEFINE_CHANNEL_RCVR: AuditOperation.MQSC_DELETE_CHANNEL,
    AuditOperation.MQSC_DEFINE_CHANNEL_SVRCONN: AuditOperation.MQSC_DELETE_CHANNEL,
}


def inverse_plan(plan: MqscPlan) -> MqscPlan:
    """Build the teardown plan for a forward `plan`.

    Strategy: walk the forward commands in REVERSE order, mapping each to
    its `rollback_text` (already encoded on every MqscCommand at forward-
    derivation time). Skip commands whose rollback is None (non-invertible
    — e.g. ALTER QMGR with no captured prior state).

    Per-command notes:
        * DEFINE QLOCAL(name) REPLACE -> DELETE QLOCAL(name)
        * DEFINE QREMOTE(name) ...    -> DELETE QREMOTE(name)
        * DEFINE QLOCAL(x) USAGE(XMITQ) -> DELETE QLOCAL(x)
        * DEFINE CHANNEL(c) CHLTYPE(SDR/RCVR/SVRCONN) -> DELETE CHANNEL(c)

    The natural delete order is the reverse of define order — channels
    first (they reference queues), then queues. Forward order was already
    queues-before-channels, so reverse is correct out of the box.

    The DLQ is intentionally preserved across teardown: it's QM-wide,
    not topology-scoped, and tearing it down doesn't fit a "realize
    THESE flows" / "tear down THESE flows" scope. Future work: a
    separate per-QM lifecycle endpoint for DLQ management.

    Args:
        plan: a forward MqscPlan produced by derive_mqsc_for_qm.

    Returns:
        A new MqscPlan whose commands are DELETE counterparts in reverse
        order. The returned plan's local_queues / remote_queues / etc.
        tuples are empty (an inverse plan doesn't introduce new objects).
    """
    inverse_commands: list[MqscCommand] = []
    for fwd in reversed(plan.commands):
        if fwd.rollback_text is None:
            # ALTER QMGR DEADQ and similar — no captured inverse.
            continue
        if fwd.object_name == "SYSTEM.DEAD.LETTER.QUEUE":
            # Preserve the DLQ — see docstring.
            continue
        inverse_op = _INVERSE_AUDIT_OP.get(fwd.op_kind)
        if inverse_op is None:
            # Forward op has no defined inverse mapping. Skip rather than
            # fabricate — better to leave the object than delete the wrong
            # thing. (Defensive: should not happen with current op set.)
            continue
        inverse_commands.append(
            MqscCommand(
                op_kind=inverse_op,
                object_kind=fwd.object_kind,
                object_name=fwd.object_name,
                mqsc_text=fwd.rollback_text,
                rationale=(
                    f"Teardown counterpart of: {fwd.rationale}"
                ),
                rollback_text=fwd.mqsc_text,  # inverse-of-inverse = forward
                related_flows=fwd.related_flows,
            )
        )

    return MqscPlan(
        qm_name=plan.qm_name,
        commands=tuple(inverse_commands),
        # Inverse plan introduces no objects; bookkeeping tuples stay empty.
        warnings=plan.warnings,
    )


def inverse_plans_for_topology(
    forward_plans: dict[str, MqscPlan],
) -> dict[str, MqscPlan]:
    """Compute inverse plans for every QM in a topology's forward-plan dict."""
    return {qm: inverse_plan(plan) for qm, plan in forward_plans.items()}


__all__ = [
    "MqscCommand",
    "MqscPlan",
    "compute_conname",
    "derive_mqsc_for_qm",
    "derive_mqsc_for_topology",
    "inverse_plan",
    "inverse_plans_for_topology",
]
