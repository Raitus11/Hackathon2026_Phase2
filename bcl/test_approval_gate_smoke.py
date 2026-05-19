"""Smoke test for the human approval gate — run this locally.

Verifies, against a real SQLite DB, that:
  1. The BCL imports and the app constructs.
  2. The DB schema creates with the new MigrationState / AuditOperation
     / AgentName enum values (no Alembic migration needed — these use
     SAEnum(native_enum=False), stored as TEXT).
  3. The decision-theory + statistical modules produce correct numbers.
  4. The new endpoints are registered.

Run from the bcl/ directory:

    BCL_LLM_PROVIDER=stub python -m pytest test_approval_gate_smoke.py -v

or directly:

    BCL_LLM_PROVIDER=stub python test_approval_gate_smoke.py

This does NOT exercise a live migration (that needs OCP + MQ pods).
It proves the gate plumbing is sound so the demo path is safe.
"""

from __future__ import annotations

import os
import sys

os.environ.setdefault("BCL_LLM_PROVIDER", "stub")


def test_imports() -> None:
    """Every new + modified module imports cleanly."""
    import bcl.api.main  # noqa: F401
    import bcl.migration.engine as engine
    import bcl.agents.preflight_auditor  # noqa: F401
    import bcl.analysis.decision  # noqa: F401
    import bcl.analysis.statistical  # noqa: F401
    import bcl.api.statistical_api  # noqa: F401

    for fn in ("resume_migration", "abort_at_gate", "re_plan_at_gate",
               "start_migration_run", "_build_runtime_context",
               "_run_compliance_narrator"):
        assert hasattr(engine, fn), f"engine missing {fn}"
    print("  [ok] all modules import; engine entry points present")


def test_enums() -> None:
    """The new enum values resolve."""
    from bcl.models.orm import AgentName, AuditOperation, MigrationState

    assert MigrationState.AWAITING_APPROVAL.value == "AWAITING_APPROVAL"
    assert AgentName.PREFLIGHT_AUDITOR.value == "PREFLIGHT_AUDITOR"
    assert AgentName.COMPLIANCE_NARRATOR.value == "COMPLIANCE_NARRATOR"
    for op in ("MIGRATION_AWAITING_APPROVAL", "MIGRATION_APPROVED",
               "MIGRATION_ABORTED", "PREFLIGHT_RISK_BRIEF"):
        assert hasattr(AuditOperation, op), f"AuditOperation missing {op}"
    print("  [ok] AWAITING_APPROVAL, 2 new agents, 4 new audit ops")


def test_state_machine() -> None:
    """The gate transitions are legal; the old illegal ones still are."""
    from bcl.migration import states
    from bcl.models.orm import MigrationState as S

    assert states.is_valid_transition(S.PLANNED, S.AWAITING_APPROVAL)
    assert states.is_valid_transition(
        S.AWAITING_APPROVAL, S.PROVISIONING_TARGET_QM
    )
    assert states.is_valid_transition(S.AWAITING_APPROVAL, S.ROLLING_BACK)
    # PLANNED can no longer jump straight to provisioning — the gate
    # is mandatory.
    assert not states.is_valid_transition(
        S.PLANNED, S.PROVISIONING_TARGET_QM
    )
    print("  [ok] gate transitions legal; gate is mandatory")


def test_decision_score() -> None:
    """The go/no-go score flips PROCEED -> DEFER as risk rises."""
    from bcl.analysis.decision import evaluate_gate

    clean = evaluate_gate([])
    assert clean.recommendation == "PROCEED"
    assert clean.advantage > 0

    risky = evaluate_gate(["CRITICAL", "HIGH"])
    assert risky.recommendation == "DEFER"
    assert risky.advantage < 0
    assert risky.expected_cost_proceed > clean.expected_cost_proceed
    print(
        f"  [ok] go/no-go: clean=PROCEED (EC={clean.expected_cost_proceed:.2f}), "
        f"critical=DEFER (EC={risky.expected_cost_proceed:.2f})"
    )


def test_statistical() -> None:
    """Welch + chi-square produce sane results on equivalent samples."""
    from bcl.analysis.statistical import chi_square_gof, welch_t_test

    # Equivalent latency samples -> fail to reject H0 -> PASS.
    pre = [0.42, 0.45, 0.39, 0.48, 0.41, 0.44]
    post = [0.43, 0.44, 0.40, 0.47, 0.42, 0.45]
    w = welch_t_test(pre, post)
    assert w.outcome == "PASS", f"expected PASS, got {w.outcome}"
    assert not w.reject_h0

    # Degraded latency -> reject H0, post higher -> FAIL.
    slow = [0.92, 0.95, 0.89, 0.98, 0.91, 0.94]
    w2 = welch_t_test(pre, slow)
    assert w2.outcome == "FAIL", f"expected FAIL, got {w2.outcome}"

    # Matching dispositions -> PASS.
    c = chi_square_gof(
        ["delivered", "dlq", "inflight"],
        [180.0, 8.0, 12.0],
        [185.0, 6.0, 9.0],
    )
    assert c.outcome == "PASS", f"expected PASS, got {c.outcome}"
    print(
        f"  [ok] Welch: equivalent=PASS (p={w.p_value:.3f}), "
        f"degraded=FAIL (p={w2.p_value:.3f}); chi-square=PASS"
    )


def test_routes() -> None:
    """The new endpoints are registered on the app."""
    from bcl.api.main import app

    paths = {r.path for r in app.routes if hasattr(r, "path")}
    for p in (
        "/migrations/{migration_id}/gate",
        "/migrations/{migration_id}/approve",
        "/migrations/{migration_id}/abort",
        "/migrations/{migration_id}/revise",
        "/statistical/welch",
        "/statistical/chi-square",
        "/statistical/validate",
    ):
        assert p in paths, f"route not registered: {p}"
    print("  [ok] 7 new endpoints registered")


async def test_schema_creates() -> None:
    """The DB schema creates with the new enum values (no migration)."""
    import tempfile

    from sqlalchemy.ext.asyncio import create_async_engine

    from bcl.models.orm import Base

    fd = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    fd.close()
    eng = create_async_engine(f"sqlite+aiosqlite:///{fd.name}")
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await eng.dispose()
    os.unlink(fd.name)
    print("  [ok] DB schema creates clean with new enum values")


def main() -> int:
    import asyncio

    print("Approval-gate smoke test")
    print("─" * 50)
    failed = 0
    sync_tests = [
        test_imports, test_enums, test_state_machine,
        test_decision_score, test_statistical, test_routes,
    ]
    for t in sync_tests:
        try:
            t()
        except Exception as exc:  # noqa: BLE001
            print(f"  [FAIL] {t.__name__}: {exc}")
            failed += 1
    try:
        asyncio.run(test_schema_creates())
    except Exception as exc:  # noqa: BLE001
        print(f"  [FAIL] test_schema_creates: {exc}")
        failed += 1

    print("─" * 50)
    if failed:
        print(f"FAILED — {failed} test(s) did not pass")
        return 1
    print("PASSED — gate plumbing is sound")
    return 0


if __name__ == "__main__":
    sys.exit(main())
