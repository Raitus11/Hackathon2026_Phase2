"""Unit tests for bcl.provisioning.k8s_client.

We exercise the subprocess code without needing a real cluster AND
without relying on Unix-specific binaries (the tests must pass on Linux,
macOS, and Windows). We use the current Python interpreter as the
"binary" with `-c "..."` to simulate echo / false / sleep.

The high-level helpers (apply_yaml, delete_resource) are tested by
asserting on K8sResult.command — the argv we WOULD have run — using a
deliberately-missing binary so the call fails fast without polluting
stdout/stderr expectations.
"""

from __future__ import annotations

import sys

import pytest

from bcl.provisioning.k8s_client import K8sClient, K8sResult


PY = sys.executable
MISSING_BINARY = "/nonexistent-on-purpose/binary-xyz"


class TestK8sClient:
    async def test_python_echo_works_for_low_level_run(self) -> None:
        """Verify _run actually executes and captures output."""
        c = K8sClient(namespace="roco-dev", binary=PY)
        result = await c._run(
            ["-c", "import sys; print(' '.join(sys.argv[1:]))",
             "apply", "-n", "roco-dev", "-f", "-"],
        )
        assert result.ok, f"unexpected failure: {result.stderr}"
        assert "apply" in result.stdout
        assert "roco-dev" in result.stdout

    async def test_apply_yaml_constructs_correct_argv(self) -> None:
        """Sanity-check argv construction without depending on binary."""
        c = K8sClient(namespace="roco-dev", binary=MISSING_BINARY)
        result = await c.apply_yaml("apiVersion: v1\nkind: ConfigMap")
        # Binary missing → call fails — but command must be right
        assert result.command == [
            MISSING_BINARY, "apply", "-n", "roco-dev", "-f", "-",
        ]
        assert not result.ok

    async def test_delete_resource_includes_ignore_not_found_flag(self) -> None:
        c = K8sClient(namespace="roco-dev", binary=MISSING_BINARY)
        result = await c.delete_resource("deployment", "qm-test")
        assert "--ignore-not-found=true" in result.command
        assert result.command[1] == "delete"
        assert "qm-test" in result.command

    async def test_delete_resource_without_ignore_flag(self) -> None:
        c = K8sClient(namespace="roco-dev", binary=MISSING_BINARY)
        result = await c.delete_resource(
            "deployment", "qm-test", ignore_not_found=False,
        )
        assert "--ignore-not-found=true" not in result.command

    async def test_failed_command_returns_nonzero_exit(self) -> None:
        c = K8sClient(namespace="roco-dev", binary=PY)
        result = await c._run(["-c", "import sys; sys.exit(1)"])
        assert not result.ok
        assert result.exit_code == 1

    async def test_timeout_kills_long_running_command(self) -> None:
        c = K8sClient(namespace="roco-dev", binary=PY)
        result = await c._run(
            ["-c", "import time; time.sleep(10)"],
            timeout=0.5,
        )
        assert not result.ok
        assert result.exit_code == -1
        assert "timed out" in result.stderr.lower()
        assert result.duration_seconds < 2.0   # killed quickly

    async def test_missing_binary_returns_error_result(self) -> None:
        c = K8sClient(namespace="roco-dev", binary=MISSING_BINARY)
        result = await c._run([])
        assert not result.ok
        assert result.exit_code == -1
        assert "not found" in result.stderr.lower()


class TestK8sResult:
    """Sync tests for K8sResult dataclass — pure data, no subprocess."""

    def test_audit_payload_keys(self) -> None:
        r = K8sResult(
            command=["oc", "apply", "-f", "-"],
            exit_code=0,
            stdout="ok",
            stderr="",
            duration_seconds=0.123,
        )
        pl = r.as_audit_payload()
        assert set(pl.keys()) == {
            "command", "exit_code", "duration_seconds",
            "stdout", "stderr",
            "stdout_truncated", "stderr_truncated",
        }
        assert pl["command"] == "oc apply -f -"
        assert pl["exit_code"] == 0
        assert pl["stdout_truncated"] is False

    def test_audit_payload_truncates_large_output(self) -> None:
        r = K8sResult(
            command=["oc"],
            exit_code=0,
            stdout="x" * (32 * 1024),
            stderr="",
            duration_seconds=0.0,
        )
        pl = r.as_audit_payload()
        assert pl["stdout_truncated"] is True
        assert len(pl["stdout"]) == 16 * 1024
