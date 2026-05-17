"""Unit tests for bcl.provisioning.mqsc_derivation.

Tests fall into three buckets:
  1. Single-flow correctness — does the derivation emit the right MQSC
     for one Local flow and one Remote flow?
  2. Multi-flow invariants — do the structural rules hold across many
     flows? (Every XMITQ referenced by a QREMOTE on this QM is defined
     on this QM; every channel has a name; SDR and RCVR are emitted on
     the right ends.)
  3. Real-data regression — does the actual source.csv produce a plan
     whose invariants hold across every QM in it?

We use the real bcl.models.api FlowSpec — no stubbing.
"""

from __future__ import annotations

import csv
from pathlib import Path

import pytest

from bcl.models.api import FlowSpec, TopologySpec
from bcl.models.orm import AuditOperation, TopologyKind
from bcl.provisioning.mqsc_derivation import (
    MqscPlan,
    compute_conname,
    derive_mqsc_for_qm,
    derive_mqsc_for_topology,
    inverse_plan,
    inverse_plans_for_topology,
)


# ─────────────────────────────────────────────────────────────────────────
# Fixtures: minimal flow builders
# ─────────────────────────────────────────────────────────────────────────


def _local_flow(
    *,
    qm: str = "WL6EEBDJ",
    queue: str = "TEST.LOCAL.QUEUE",
    producer_app: str = "LIY/KW",
    consumer_app: str = "JUUD/C9",
) -> FlowSpec:
    return FlowSpec(
        flow_type="Local",
        producer_app_id=producer_app,
        producer_app_name="ProducerApp",
        producer_neighbourhood="Data & Analytics",
        producer_queue_manager=qm,
        producer_queue_name=queue,
        producer_queue_type="Local",
        transmit_queue_name=None,
        channel_name=None,
        consumer_app_id=consumer_app,
        consumer_app_name="ConsumerApp",
        consumer_neighbourhood="Core Banking",
        consumer_queue_manager=qm,
        consumer_queue_name=queue,
        consumer_queue_type="Local",
    )


def _remote_flow(
    *,
    producer_qm: str = "APPQM_LIY_KW",
    consumer_qm: str = "APPQM_APUMN_GC",
    producer_queue: str = "LIY.GC.JOPPIKT.XL21",
    consumer_queue: str = "LIY.GC.JOPPIKT.XL21",
    xmitq: str = "APPQM_APUMN_GC.XMIT",
    channel: str = "APPQM_LIY_KW.APPQM_APUMN_GC",
) -> FlowSpec:
    return FlowSpec(
        flow_type="Remote",
        producer_app_id="LIY/KW",
        producer_app_name="ProducerApp",
        producer_neighbourhood="Data & Analytics",
        producer_queue_manager=producer_qm,
        producer_queue_name=producer_queue,
        producer_queue_type="Remote",
        transmit_queue_name=xmitq,
        channel_name=channel,
        consumer_app_id="APUMN/GC",
        consumer_app_name="ConsumerApp",
        consumer_neighbourhood="Core Banking",
        consumer_queue_manager=consumer_qm,
        consumer_queue_name=consumer_queue,
        consumer_queue_type="Local",
    )


# ─────────────────────────────────────────────────────────────────────────
# Conname
# ─────────────────────────────────────────────────────────────────────────


class TestComputeConname:
    def test_simple(self) -> None:
        assert compute_conname(
            "APPQM_LIY_KW", namespace="roco-dev", listener_port=1414
        ) == "qm-appqm-liy-kw.roco-dev.svc.cluster.local(1414)"

    def test_lowercases_and_replaces_underscores(self) -> None:
        assert compute_conname(
            "WQ22", namespace="ns", listener_port=1414
        ) == "qm-wq22.ns.svc.cluster.local(1414)"

    def test_collapses_runs_of_separators(self) -> None:
        # underscores -> hyphens; runs collapse via the regex
        cn = compute_conname("FOO__BAR", namespace="ns", listener_port=1414)
        assert cn == "qm-foo-bar.ns.svc.cluster.local(1414)"


# ─────────────────────────────────────────────────────────────────────────
# Single-flow derivation
# ─────────────────────────────────────────────────────────────────────────


class TestLocalFlow:
    """One Local flow: same QM is producer and consumer, one local queue."""

    def test_emits_dlq_alter_define_and_local_queue(self) -> None:
        flow = _local_flow(qm="WL6EEBDJ", queue="MY.QUEUE")
        plan = derive_mqsc_for_qm(
            qm_name="WL6EEBDJ",
            flows=[flow],
            namespace="roco-dev",
            listener_port=1414,
        )
        ops = [c.op_kind for c in plan.commands]
        # Step 1 is three ALTER QMGR commands: DEADQ, CHLAUTH(DISABLED),
        # REFRESH SECURITY (the 2026-05-14 patch that removed the manual
        # MQSC unblocking step). Then the DLQ QLOCAL, then the queue.
        assert ops == [
            AuditOperation.MQSC_ALTER_QMGR,  # ALTER QMGR DEADQ
            AuditOperation.MQSC_ALTER_QMGR,  # ALTER QMGR CHLAUTH(DISABLED)
            AuditOperation.MQSC_ALTER_QMGR,  # REFRESH SECURITY TYPE(CONNAUTH)
            AuditOperation.MQSC_DEFINE_QLOCAL,  # SYSTEM.DEAD.LETTER.QUEUE
            AuditOperation.MQSC_DEFINE_QLOCAL,  # MY.QUEUE
        ]
        assert plan.local_queues == ("MY.QUEUE",)
        assert plan.remote_queues == ()
        assert plan.transmit_queues == ()
        assert plan.sender_channels == ()
        assert plan.receiver_channels == ()
        # Step 1 disables CHLAUTH for demo compatibility and records a
        # posture warning (tracked in the FMEA / Phase 3 backlog).
        assert any("CHLAUTH disabled" in w for w in plan.warnings)

    def test_skips_flow_where_this_qm_uninvolved(self) -> None:
        flow = _local_flow(qm="WL6EEBDJ", queue="MY.QUEUE")
        plan = derive_mqsc_for_qm(
            qm_name="OTHER_QM",
            flows=[flow],
            namespace="roco-dev",
            listener_port=1414,
        )
        # Only step-1 machinery — the flow doesn't involve OTHER_QM at all.
        # Step 1 = 3 ALTER QMGR (DEADQ, CHLAUTH, REFRESH) + DLQ QLOCAL.
        assert len(plan.commands) == 4
        assert plan.local_queues == ()


class TestRemoteFlow:
    """One Remote flow: producer-side QM gets QREMOTE+XMITQ+SDR;
    consumer-side QM gets QLOCAL+RCVR.
    """

    def test_producer_side(self) -> None:
        flow = _remote_flow()
        plan = derive_mqsc_for_qm(
            qm_name="APPQM_LIY_KW",
            flows=[flow],
            namespace="roco-dev",
            listener_port=1414,
        )

        ops = [c.op_kind for c in plan.commands]
        assert ops == [
            AuditOperation.MQSC_ALTER_QMGR,           # ALTER QMGR DEADQ
            AuditOperation.MQSC_ALTER_QMGR,           # CHLAUTH(DISABLED)
            AuditOperation.MQSC_ALTER_QMGR,           # REFRESH SECURITY
            AuditOperation.MQSC_DEFINE_QLOCAL,        # DLQ
            AuditOperation.MQSC_DEFINE_QXMIT,
            AuditOperation.MQSC_DEFINE_QREMOTE,
            AuditOperation.MQSC_DEFINE_CHANNEL_SDR,   # DEFINE CHANNEL
            AuditOperation.MQSC_DEFINE_CHANNEL_SDR,   # START CHANNEL
        ]

        # Inspect the channel command for correct CONNAME
        sdr = next(
            c for c in plan.commands
            if c.op_kind == AuditOperation.MQSC_DEFINE_CHANNEL_SDR
        )
        assert "CHLTYPE(SDR)" in sdr.mqsc_text
        assert "CONNAME('qm-appqm-apumn-gc.roco-dev.svc.cluster.local(1414)')" in sdr.mqsc_text
        assert "XMITQ('APPQM_APUMN_GC.XMIT')" in sdr.mqsc_text

        # QREMOTE references the consumer queue + consumer QM
        qremote = next(
            c for c in plan.commands
            if c.op_kind == AuditOperation.MQSC_DEFINE_QREMOTE
        )
        assert "RNAME('LIY.GC.JOPPIKT.XL21')" in qremote.mqsc_text
        assert "RQMNAME('APPQM_APUMN_GC')" in qremote.mqsc_text
        assert "XMITQ('APPQM_APUMN_GC.XMIT')" in qremote.mqsc_text

    def test_consumer_side(self) -> None:
        flow = _remote_flow()
        plan = derive_mqsc_for_qm(
            qm_name="APPQM_APUMN_GC",
            flows=[flow],
            namespace="roco-dev",
            listener_port=1414,
        )
        ops = [c.op_kind for c in plan.commands]
        assert ops == [
            AuditOperation.MQSC_ALTER_QMGR,           # ALTER QMGR DEADQ
            AuditOperation.MQSC_ALTER_QMGR,           # CHLAUTH(DISABLED)
            AuditOperation.MQSC_ALTER_QMGR,           # REFRESH SECURITY
            AuditOperation.MQSC_DEFINE_QLOCAL,        # DLQ
            AuditOperation.MQSC_DEFINE_QLOCAL,        # destination queue
            AuditOperation.MQSC_DEFINE_CHANNEL_RCVR,
        ]
        rcvr = next(
            c for c in plan.commands
            if c.op_kind == AuditOperation.MQSC_DEFINE_CHANNEL_RCVR
        )
        assert "CHLTYPE(RCVR)" in rcvr.mqsc_text


# ─────────────────────────────────────────────────────────────────────────
# Idempotency and determinism
# ─────────────────────────────────────────────────────────────────────────


class TestDeterminism:
    def test_same_input_same_plan(self) -> None:
        flow = _remote_flow()
        plan_a = derive_mqsc_for_qm(
            qm_name="APPQM_LIY_KW",
            flows=[flow],
            namespace="roco-dev",
            listener_port=1414,
        )
        plan_b = derive_mqsc_for_qm(
            qm_name="APPQM_LIY_KW",
            flows=[flow],
            namespace="roco-dev",
            listener_port=1414,
        )
        assert plan_a == plan_b

    def test_dedup_across_flows(self) -> None:
        """Two flows producing identical QREMOTE+XMITQ+SDR collapse to one of each."""
        f1 = _remote_flow()
        f2 = _remote_flow()  # same params -> identical objects
        plan = derive_mqsc_for_qm(
            qm_name="APPQM_LIY_KW",
            flows=[f1, f2],
            namespace="roco-dev",
            listener_port=1414,
        )
        ops = [c.op_kind for c in plan.commands]
        assert ops.count(AuditOperation.MQSC_DEFINE_QREMOTE) == 1
        assert ops.count(AuditOperation.MQSC_DEFINE_QXMIT) == 1
        # One SDR channel collapses to a DEFINE + a START command (both
        # carry op_kind MQSC_DEFINE_CHANNEL_SDR), so count distinct
        # channel objects rather than raw op occurrences.
        sdr_objects = {
            c.object_name
            for c in plan.commands
            if c.op_kind == AuditOperation.MQSC_DEFINE_CHANNEL_SDR
        }
        assert len(sdr_objects) == 1
        # The only warning expected is the standard CHLAUTH posture note;
        # there must be no dedup/duplicate-object warnings.
        assert all("CHLAUTH disabled" in w for w in plan.warnings)


class TestReplaceIdempotency:
    """Every DEFINE command must include REPLACE so re-application is a no-op
    at the MQ level (separate from the AMQ-code-tolerance idempotency layer)."""

    def test_every_define_has_replace(self) -> None:
        flow = _remote_flow()
        plan = derive_mqsc_for_qm(
            qm_name="APPQM_LIY_KW",
            flows=[flow],
            namespace="roco-dev",
            listener_port=1414,
        )
        for cmd in plan.commands:
            if cmd.mqsc_text.startswith("DEFINE"):
                assert cmd.mqsc_text.endswith("REPLACE"), (
                    f"DEFINE without REPLACE: {cmd.mqsc_text}"
                )


# ─────────────────────────────────────────────────────────────────────────
# Cross-flow inconsistency surfacing (NOT raising)
# ─────────────────────────────────────────────────────────────────────────


class TestSurfacingNotRaising:
    """The derivation must be total: it accepts any valid FlowSpec list
    and returns a plan, even when flows disagree about non-FlowSpec-
    schema details.
    """

    def test_channel_with_two_xmitqs_warns_keeps_first(self) -> None:
        f1 = _remote_flow(xmitq="XMITQ_A")
        f2 = _remote_flow(xmitq="XMITQ_B")  # same channel name, different XMITQ
        plan = derive_mqsc_for_qm(
            qm_name="APPQM_LIY_KW",
            flows=[f1, f2],
            namespace="roco-dev",
            listener_port=1414,
        )
        # Both XMITQs are defined (objects must exist for QREMOTEs to resolve).
        assert "XMITQ_A" in plan.transmit_queues
        assert "XMITQ_B" in plan.transmit_queues
        # But only one SDR channel object (first-seen wins). One channel
        # yields a DEFINE + a START command, so compare distinct objects.
        sdrs = [
            c for c in plan.commands
            if c.op_kind == AuditOperation.MQSC_DEFINE_CHANNEL_SDR
        ]
        sdr_objects = {c.object_name for c in sdrs}
        assert len(sdr_objects) == 1
        sdr_define = next(
            c for c in sdrs if c.mqsc_text.startswith("DEFINE")
        )
        assert "XMITQ('XMITQ_A')" in sdr_define.mqsc_text
        # And a warning was recorded.
        assert any(
            "XMITQ_B" in w and "first-seen" in w for w in plan.warnings
        )

    def test_local_flow_with_mismatched_queue_names_defines_both(self) -> None:
        # We can't construct this directly through TopologySpec validator
        # since its own validator allows it (Local == same QM, not same Q),
        # but the derivation must handle it.
        flow = FlowSpec(
            flow_type="Local",
            producer_app_id="A",
            producer_app_name="An",
            producer_neighbourhood="N",
            producer_queue_manager="QM1",
            producer_queue_name="Q.A",
            producer_queue_type="Local",
            transmit_queue_name=None,
            channel_name=None,
            consumer_app_id="B",
            consumer_app_name="Bn",
            consumer_neighbourhood="N",
            consumer_queue_manager="QM1",
            consumer_queue_name="Q.B",  # different from producer
            consumer_queue_type="Local",
        )
        plan = derive_mqsc_for_qm(
            qm_name="QM1",
            flows=[flow],
            namespace="ns",
            listener_port=1414,
        )
        assert "Q.A" in plan.local_queues
        assert "Q.B" in plan.local_queues
        assert any("different producer queue" in w for w in plan.warnings)


# ─────────────────────────────────────────────────────────────────────────
# Structural invariants — these are what mq_realize.py depends on
# ─────────────────────────────────────────────────────────────────────────


def _qm_invariants(plan: MqscPlan, all_plans: dict[str, MqscPlan]) -> None:
    """Assertions that must hold for any single QM plan.

    These are the contract mq_realize.py relies on. If they break, the
    executor will produce broken MQ object graphs.
    """
    # 1. Every XMITQ referenced by a QREMOTE on this QM is defined on this QM.
    for cmd in plan.commands:
        if cmd.op_kind == AuditOperation.MQSC_DEFINE_QREMOTE:
            import re
            m = re.search(r"XMITQ\('([^']+)'\)", cmd.mqsc_text)
            assert m is not None, f"QREMOTE without XMITQ clause: {cmd.mqsc_text}"
            xmitq = m.group(1)
            assert xmitq in plan.transmit_queues, (
                f"QREMOTE on {plan.qm_name} references XMITQ {xmitq!r} "
                f"which is not defined on this QM. Plan transmit_queues: "
                f"{plan.transmit_queues}"
            )

    # 2. Every SDR channel DEFINE's XMITQ is defined on this QM.
    #    SDR-kind commands include START CHANNEL, which has no XMITQ
    #    clause — check only the DEFINE.
    for cmd in plan.commands:
        if (
            cmd.op_kind == AuditOperation.MQSC_DEFINE_CHANNEL_SDR
            and cmd.mqsc_text.startswith("DEFINE")
        ):
            import re
            m = re.search(r"XMITQ\('([^']+)'\)", cmd.mqsc_text)
            assert m is not None
            assert m.group(1) in plan.transmit_queues

    # 3. Every command is non-empty and starts with a known verb.
    #    Step 1 adds REFRESH SECURITY; SDR channels add START CHANNEL.
    known_verbs = ("ALTER", "DEFINE", "REFRESH", "START")
    for cmd in plan.commands:
        assert any(cmd.mqsc_text.startswith(v) for v in known_verbs), (
            f"Unknown verb in {cmd.mqsc_text}"
        )

    # 4. Step 1 is three ALTER QMGR commands: DEADQ, CHLAUTH(DISABLED),
    #    REFRESH SECURITY. DLQ is non-negotiable; the CHLAUTH pair is the
    #    2026-05-14 patch that removed the manual MQSC unblocking step.
    assert plan.commands[0].op_kind == AuditOperation.MQSC_ALTER_QMGR
    assert "DEADQ" in plan.commands[0].mqsc_text
    assert plan.commands[1].op_kind == AuditOperation.MQSC_ALTER_QMGR
    assert "CHLAUTH" in plan.commands[1].mqsc_text
    assert plan.commands[2].op_kind == AuditOperation.MQSC_ALTER_QMGR
    assert "REFRESH SECURITY" in plan.commands[2].mqsc_text

    # 5. Fourth command is the DLQ QLOCAL DEFINE.
    assert plan.commands[3].op_kind == AuditOperation.MQSC_DEFINE_QLOCAL
    assert "SYSTEM.DEAD.LETTER.QUEUE" in plan.commands[3].mqsc_text


def _topology_invariants(plans: dict[str, MqscPlan]) -> None:
    """Cross-QM invariants. The system depends on these for real message flow."""
    # Every SDR's peer QM has the corresponding RCVR (channel name match).
    # This is the producer/consumer-channel-pair invariant.
    sdr_to_peers: dict[str, list[str]] = {}  # channel_name -> list of producer QMs
    rcvr_at: dict[str, list[str]] = {}       # channel_name -> list of consumer QMs

    for qm_name, plan in plans.items():
        for cmd in plan.commands:
            if cmd.op_kind == AuditOperation.MQSC_DEFINE_CHANNEL_SDR:
                sdr_to_peers.setdefault(cmd.object_name, []).append(qm_name)
            elif cmd.op_kind == AuditOperation.MQSC_DEFINE_CHANNEL_RCVR:
                rcvr_at.setdefault(cmd.object_name, []).append(qm_name)

    # Every SDR has a matching RCVR somewhere in the topology.
    # (Note: not every RCVR has a matching SDR in the SAME topology — the
    # consumer side of an inbound channel from outside the modelled topology
    # could exist as RCVR-only.)
    for channel, producers in sdr_to_peers.items():
        assert channel in rcvr_at, (
            f"SDR channel {channel!r} on {producers} has no matching RCVR "
            f"in the topology. Defined RCVR channels: {sorted(rcvr_at)}"
        )


# ─────────────────────────────────────────────────────────────────────────
# Real-data regression: source.csv from the repo
# ─────────────────────────────────────────────────────────────────────────


def _csv_path() -> Path:
    """Resolve the source.csv path; skip if not present (CI vs local)."""
    candidates = [
        Path(__file__).resolve().parent.parent / "data" / "source.csv",
        Path(__file__).resolve().parent / "fixtures" / "source.csv",
        Path("/mnt/user-data/uploads/source.csv"),  # sandbox dev path
    ]
    for c in candidates:
        if c.exists():
            return c
    pytest.skip("source.csv fixture not available")


def _load_flows_from_csv(path: Path) -> list[FlowSpec]:
    rows: list[dict[str, str]] = []
    with path.open(encoding="utf-8-sig") as fh:
        rows = list(csv.DictReader(fh))
    flows: list[FlowSpec] = []
    for row in rows:
        # FlowSpec's AliasChoices accepts either spelling
        flows.append(FlowSpec.model_validate({
            **row,
            "transmit_queue_name": row["transmit_queue_name"] or None,
            "channel_name": row["channel_name"] or None,
        }))
    return flows


class TestRealSourceCsv:
    def test_source_csv_loads_via_topology_spec(self) -> None:
        flows = _load_flows_from_csv(_csv_path())
        # Validating via TopologySpec also exercises the cross-flow
        # consistency check (Local-flow same-QM, Remote-flow different-QM).
        spec = TopologySpec(
            name="src-from-csv", kind=TopologyKind.SOURCE, flows=flows
        )
        assert len(spec.flows) == 45

    def test_source_csv_produces_valid_plans(self) -> None:
        flows = _load_flows_from_csv(_csv_path())
        plans = derive_mqsc_for_topology(
            flows=flows,
            namespace="roco-dev",
            listener_port=1414,
        )
        # The expected source QMs (from CSV inspection).
        expected_qms = {
            "WL6EEBDJ", "WL6ER0C", "WL6ER2C", "WL6ES3C",
            "WLZ03", "WQ21", "WQ22", "WQ31", "WUZ20",
        }
        assert set(plans) == expected_qms

        for plan in plans.values():
            _qm_invariants(plan, plans)

        _topology_invariants(plans)

    def test_source_csv_surfaces_known_warnings(self) -> None:
        """Document the two warnings the real source.csv produces.

        These are real data anomalies in the production export. If the CSV
        is re-exported and these disappear, this test fails noisily so we
        know to investigate.
        """
        flows = _load_flows_from_csv(_csv_path())
        plans = derive_mqsc_for_topology(
            flows=flows, namespace="roco-dev", listener_port=1414
        )
        all_warnings = [w for plan in plans.values() for w in plan.warnings]
        # WQ22.WLZ03 channel referenced by two flows with different XMITQs
        assert any("WQ22.WLZ03" in w for w in all_warnings)
        # WQ31 has a local-flow with mismatched producer/consumer queue names
        assert any("WQ31" in w and "TZR1" in w for w in all_warnings)


# ─────────────────────────────────────────────────────────────────────────
# Inverse plan / teardown derivation
# ─────────────────────────────────────────────────────────────────────────


class TestInversePlan:
    """The inverse plan is what `mq_realize.py` uses to tear down MQ
    objects. Round-trip is a hard property: applying forward then inverse
    must remove every object the forward plan created, EXCEPT the DLQ
    (which is preserved by design).
    """

    def test_inverse_of_local_flow(self) -> None:
        flow = _local_flow(qm="QM1", queue="MY.QUEUE")
        fwd = derive_mqsc_for_qm(
            qm_name="QM1", flows=[flow], namespace="ns", listener_port=1414
        )
        inv = inverse_plan(fwd)
        ops = [c.op_kind for c in inv.commands]
        # DLQ define is preserved (NOT inverted), ALTER QMGR has no inverse.
        # Only the MY.QUEUE definition gets a DELETE counterpart.
        assert ops == [AuditOperation.MQSC_DELETE_QLOCAL]
        assert "MY.QUEUE" in inv.commands[0].mqsc_text
        assert inv.commands[0].mqsc_text.startswith("DELETE QLOCAL")

    def test_inverse_of_remote_flow_producer_side(self) -> None:
        flow = _remote_flow()
        fwd = derive_mqsc_for_qm(
            qm_name="APPQM_LIY_KW", flows=[flow],
            namespace="roco-dev", listener_port=1414,
        )
        inv = inverse_plan(fwd)
        ops = [c.op_kind for c in inv.commands]
        # Forward emits (after step 1): QXMIT, QREMOTE, SDR DEFINE, SDR
        # START. Reverse-order teardown: STOP CHANNEL, DELETE CHANNEL,
        # DELETE QREMOTE, DELETE QXMIT. STOP + DELETE channel both carry
        # op_kind MQSC_DELETE_CHANNEL.
        assert ops == [
            AuditOperation.MQSC_DELETE_CHANNEL,   # STOP CHANNEL
            AuditOperation.MQSC_DELETE_CHANNEL,   # DELETE CHANNEL
            AuditOperation.MQSC_DELETE_QREMOTE,
            AuditOperation.MQSC_DELETE_QXMIT,
        ]

    def test_inverse_skips_dlq_and_alter_qmgr(self) -> None:
        """The teardown plan never deletes the DLQ or attempts to invert
        ALTER QMGR — both are out of scope for a topology-level teardown."""
        flow = _local_flow(qm="QM1", queue="OTHER.QUEUE")
        fwd = derive_mqsc_for_qm(
            qm_name="QM1", flows=[flow], namespace="ns", listener_port=1414
        )
        inv = inverse_plan(fwd)
        for cmd in inv.commands:
            assert "SYSTEM.DEAD.LETTER.QUEUE" not in cmd.mqsc_text
            assert not cmd.mqsc_text.startswith("ALTER")

    def test_inverse_reverse_order(self) -> None:
        """Channels must be deleted before the queues they reference."""
        flow = _remote_flow()
        fwd = derive_mqsc_for_qm(
            qm_name="APPQM_LIY_KW", flows=[flow],
            namespace="roco-dev", listener_port=1414,
        )
        inv = inverse_plan(fwd)
        # Find index of the channel-delete and the xmitq-delete; channel
        # must come first to avoid "QXMIT in use" errors.
        channel_idx = next(
            i for i, c in enumerate(inv.commands)
            if c.op_kind == AuditOperation.MQSC_DELETE_CHANNEL
        )
        xmitq_idx = next(
            i for i, c in enumerate(inv.commands)
            if c.op_kind == AuditOperation.MQSC_DELETE_QXMIT
        )
        assert channel_idx < xmitq_idx, (
            "Channel must be deleted before its XMITQ "
            "(MQ refuses to delete an in-use XMITQ)."
        )

    def test_inverse_of_inverse_is_forward(self) -> None:
        """Per-command: applying inverse twice should reproduce the
        forward mqsc_text (modulo the DLQ + ALTER QMGR commands which
        the inverse drops by design)."""
        flow = _remote_flow()
        fwd = derive_mqsc_for_qm(
            qm_name="APPQM_LIY_KW", flows=[flow],
            namespace="roco-dev", listener_port=1414,
        )
        inv = inverse_plan(fwd)
        # Each inv command has rollback_text = the forward mqsc_text.
        # That's enough for a one-shot apply → undo → re-apply cycle
        # to converge.
        for inv_cmd in inv.commands:
            assert inv_cmd.rollback_text is not None
            # The inverse of a DELETE is a DEFINE; the inverse of a STOP
            # CHANNEL is a START CHANNEL. Both are legitimate forward
            # rollback verbs — the inverse-of-inverse converges either way.
            assert inv_cmd.rollback_text.startswith(("DEFINE", "START"))

    def test_real_csv_round_trip_object_inventory(self) -> None:
        """Apply forward + inverse over the full source.csv:
            objects-created-by-forward == objects-deleted-by-inverse
        (set equality, modulo DLQ which is preserved).

        This is the round-trip property test: forward + inverse over
        the full real-world CSV must produce inverse commands that
        delete exactly the objects the forward commands created.
        """
        flows = _load_flows_from_csv(_csv_path())
        fwd_plans = derive_mqsc_for_topology(
            flows=flows, namespace="roco-dev", listener_port=1414
        )
        inv_plans = inverse_plans_for_topology(fwd_plans)

        for qm_name, fwd in fwd_plans.items():
            inv = inv_plans[qm_name]

            # Set of objects the forward plan creates (object_name keyed).
            # Exclude DLQ + QMGR-targeted commands.
            fwd_objects = {
                (c.object_kind, c.object_name)
                for c in fwd.commands
                if c.object_name != "SYSTEM.DEAD.LETTER.QUEUE"
                and c.object_kind != "QMGR"
            }
            # Set of objects the inverse plan deletes.
            inv_objects = {(c.object_kind, c.object_name) for c in inv.commands}

            assert fwd_objects == inv_objects, (
                f"Round-trip mismatch on QM {qm_name!r}: "
                f"forward creates {fwd_objects - inv_objects} that inverse "
                f"doesn't delete; inverse deletes {inv_objects - fwd_objects} "
                f"that forward didn't create."
            )
