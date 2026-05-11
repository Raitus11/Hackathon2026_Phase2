"""Integration test for the provisioning engine.

Uses an in-memory SQLite database and the current Python interpreter
as the K8s binary so the full engine code path runs without needing a
real cluster, and without relying on Unix-specific binaries.

Verifies:
  - State transitions PENDING → RUNNING → COMPLETED
  - One audit entry per apply, Lamport-monotonic
  - QueueManager row's pod_name / service_name / is_ready get updated
  - Per-QM progress events captured in run.progress
  - Dry-run mode bypasses K8s and marks events as DRY_RUN
"""

from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from bcl.audit.lamport import LamportClock
from bcl.models.orm import (
    AuditLog,
    AuditOperation,
    Base,
    ProvisionState,
    QueueManager,
    Topology,
    TopologyKind,
)
from bcl.provisioning import engine
from bcl.provisioning.k8s_client import K8sResult


@pytest.fixture(autouse=True)
def use_python_as_binary(monkeypatch: pytest.MonkeyPatch) -> None:
    """Cross-platform: use the running Python interpreter as the 'binary'.

    For these tests the binary is never actually invoked (apply_yaml and
    wait_for_deployment_available are mocked), but K8sClient construction
    calls _resolve_binary() which validates the binary exists on PATH.
    sys.executable is guaranteed to exist on any platform we run on.
    """
    monkeypatch.setenv("BCL_K8S_BINARY", sys.executable)


@pytest.fixture
async def factory() -> async_sessionmaker[AsyncSession]:
    eng = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(eng, class_=AsyncSession, expire_on_commit=False)
    LamportClock.reset_for_tests()
    async with factory() as s:
        await LamportClock.instance().bootstrap(s)
    return factory


async def _seed_topology(
    factory: async_sessionmaker[AsyncSession],
    qm_names: list[str],
    name: str = "test-topology",
) -> int:
    async with factory() as s:
        t = Topology(
            name=name, kind=TopologyKind.TARGET, spec={"flows": []},
            created_at=datetime.now(UTC),
        )
        s.add(t)
        await s.flush()
        for qm in qm_names:
            s.add(QueueManager(topology_id=t.id, qm_name=qm, is_ready=False))
        await s.commit()
        return t.id


async def _wait_for_run(
    factory: async_sessionmaker[AsyncSession],
    run_id: str,
    timeout_s: float = 10.0,
) -> None:
    """Poll until the run reaches a terminal state."""
    deadline = asyncio.get_running_loop().time() + timeout_s
    while asyncio.get_running_loop().time() < deadline:
        async with factory() as s:
            r = await engine.get_run(s, run_id)
            if r and r.state in (
                ProvisionState.COMPLETED,
                ProvisionState.FAILED,
                ProvisionState.PARTIALLY_COMPLETED,
            ):
                return
        await asyncio.sleep(0.1)
    raise TimeoutError(f"run {run_id} did not finish within {timeout_s}s")


class TestDryRun:
    async def test_completes_with_dry_run_events(
        self, factory: async_sessionmaker[AsyncSession]
    ) -> None:
        topology_id = await _seed_topology(factory, ["SRC_QM_CB", "SRC_QM_RB"])

        async with factory() as s:
            run = await engine.start_provision_run(
                s, topology_id=topology_id, actor="operator:test",
                operator_message="dry-run integration test",
                session_factory=factory, dry_run=True,
            )
            run_id = run.run_id
            assert run.state == ProvisionState.PENDING
            assert run.qms_total == 2

        await _wait_for_run(factory, run_id)

        async with factory() as s:
            r = await engine.get_run(s, run_id)
            assert r.state == ProvisionState.COMPLETED
            assert r.qms_ready == 2
            assert r.qms_failed == 0
            # 4 events per QM × 2 QMs = 8 total
            assert len(r.progress) == 8, (
                f"expected 8 events, got {len(r.progress)}:\n"
                + "\n".join(f"  {p['qm_name']}/{p['phase']}/{p['status']}" for p in r.progress)
            )
            assert all(e["status"] == "DRY_RUN" for e in r.progress)


class TestLiveModeWithMockedK8s:
    """Live-mode means the engine sends commands to the binary; we mock
    the high-level operations to return success without a real cluster."""

    async def test_qm_marked_ready_after_full_flow(
        self, factory: async_sessionmaker[AsyncSession]
    ) -> None:
        topology_id = await _seed_topology(factory, ["APUMN_GC_QM"])

        fake_ok = K8sResult(
            command=["fake"], exit_code=0, stdout="", stderr="",
            duration_seconds=0.01,
        )

        with patch(
            "bcl.provisioning.k8s_client.K8sClient.apply_yaml",
            new=AsyncMock(return_value=fake_ok),
        ), patch(
            "bcl.provisioning.k8s_client.K8sClient.wait_for_deployment_available",
            new=AsyncMock(return_value=fake_ok),
        ), patch(
            "bcl.provisioning.k8s_client.K8sClient.get_pod_name",
            new=AsyncMock(return_value=(fake_ok, "qm-apumn-gc-qm-xyz")),
        ):
            async with factory() as s:
                run = await engine.start_provision_run(
                    s, topology_id=topology_id, actor="operator:test",
                    operator_message=None, session_factory=factory, dry_run=False,
                )
                run_id = run.run_id

            await _wait_for_run(factory, run_id)

        async with factory() as s:
            r = await engine.get_run(s, run_id)
            assert r.state == ProvisionState.COMPLETED
            assert r.qms_ready == 1
            assert r.qms_failed == 0

            qm = (await s.execute(
                select(QueueManager).where(QueueManager.qm_name == "APUMN_GC_QM")
            )).scalar_one()
            assert qm.is_ready is True
            assert qm.pod_name == "qm-apumn-gc-qm-xyz"
            assert qm.service_name is not None

    async def test_audit_log_has_one_entry_per_step(
        self, factory: async_sessionmaker[AsyncSession]
    ) -> None:
        topology_id = await _seed_topology(factory, ["APUMN_GC_QM"])
        fake_ok = K8sResult(
            command=["fake"], exit_code=0, stdout="", stderr="",
            duration_seconds=0.01,
        )

        with patch(
            "bcl.provisioning.k8s_client.K8sClient.apply_yaml",
            new=AsyncMock(return_value=fake_ok),
        ), patch(
            "bcl.provisioning.k8s_client.K8sClient.wait_for_deployment_available",
            new=AsyncMock(return_value=fake_ok),
        ), patch(
            "bcl.provisioning.k8s_client.K8sClient.get_pod_name",
            new=AsyncMock(return_value=(fake_ok, "qm-apumn-gc-qm-xyz")),
        ):
            async with factory() as s:
                run = await engine.start_provision_run(
                    s, topology_id=topology_id, actor="operator:audit-test",
                    operator_message=None, session_factory=factory, dry_run=False,
                )
                run_id = run.run_id

            await _wait_for_run(factory, run_id)

        async with factory() as s:
            audits = (await s.execute(
                select(AuditLog).order_by(AuditLog.lamport_clock)
            )).scalars().all()

            ops = [a.operation for a in audits]
            assert AuditOperation.PROVISION_STARTED in ops
            assert AuditOperation.QM_PVC_CREATED in ops
            assert AuditOperation.QM_SECRET_CREATED in ops
            assert AuditOperation.QM_DEPLOYED in ops
            assert AuditOperation.QM_SERVICE_CREATED in ops
            assert AuditOperation.QM_READY in ops
            assert AuditOperation.PROVISION_COMPLETED in ops

            clocks = [a.lamport_clock for a in audits]
            assert clocks == sorted(clocks)
            assert len(set(clocks)) == len(clocks)

            run_corr = (await engine.get_run(s, run_id)).correlation_id
            assert all(a.correlation_id == run_corr for a in audits)


class TestFailurePaths:
    async def test_missing_topology_raises(
        self, factory: async_sessionmaker[AsyncSession]
    ) -> None:
        async with factory() as s:
            with pytest.raises(LookupError):
                await engine.start_provision_run(
                    s, topology_id=9999, actor="op", operator_message=None,
                    session_factory=factory, dry_run=True,
                )

    async def test_topology_with_no_qms_raises(
        self, factory: async_sessionmaker[AsyncSession]
    ) -> None:
        async with factory() as s:
            t = Topology(
                name="empty", kind=TopologyKind.SOURCE, spec={"flows": []},
                created_at=datetime.now(UTC),
            )
            s.add(t)
            await s.commit()
            tid = t.id

        async with factory() as s:
            with pytest.raises(ValueError, match="no queue managers"):
                await engine.start_provision_run(
                    s, topology_id=tid, actor="op", operator_message=None,
                    session_factory=factory, dry_run=True,
                )

    async def test_apply_failure_marks_run_failed(
        self, factory: async_sessionmaker[AsyncSession]
    ) -> None:
        topology_id = await _seed_topology(factory, ["FAILING_QM"])

        fake_fail = K8sResult(
            command=["fake"], exit_code=1, stdout="",
            stderr="forbidden by some policy",
            duration_seconds=0.01,
        )
        with patch(
            "bcl.provisioning.k8s_client.K8sClient.apply_yaml",
            new=AsyncMock(return_value=fake_fail),
        ):
            async with factory() as s:
                run = await engine.start_provision_run(
                    s, topology_id=topology_id, actor="op", operator_message=None,
                    session_factory=factory, dry_run=False,
                )
                run_id = run.run_id
            await _wait_for_run(factory, run_id)

        async with factory() as s:
            r = await engine.get_run(s, run_id)
            assert r.state == ProvisionState.FAILED
            assert r.qms_failed == 1
            assert r.qms_ready == 0
            assert r.error is not None
