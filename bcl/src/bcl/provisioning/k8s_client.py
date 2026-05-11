"""Kubernetes client — subprocess wrapper around `oc` / `kubectl`.

Marcus's reasoning for subprocess vs the official kubernetes-client library:
1. Audit-log entries can show the literal command an operator would have
   typed at a terminal — judges see something they recognize.
2. We inherit operator-parity in failure modes: same RBAC denies, same
   image-pull errors, same `oc describe` outputs.
3. Fewer moving parts to debug at 3am.

This module:
- Locates the `oc` binary (falls back to `kubectl`).
- Wraps `apply`, `delete`, `get`, `wait` with structured input/output.
- Captures the full stdout/stderr/exit-code per call so callers can
  audit-log the exact result.
- Async via `asyncio.create_subprocess_exec` — provisioning runs are
  background tasks; no thread pool needed.
- Returns dataclasses, never raw subprocess types, so callers test cleanly.

This module is the ONLY place in BCL that shells out to K8s. Everywhere
else goes through these functions.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("bcl.provisioning.k8s_client")


# ─────────────────────────────────────────────────────────────────────────
# Binary discovery
# ─────────────────────────────────────────────────────────────────────────


def _resolve_binary() -> str:
    """Find the kubernetes CLI to use.

    Order: BCL_K8S_BINARY env var > oc > kubectl. Raises FileNotFoundError
    if neither is on PATH.
    """
    env_override = os.environ.get("BCL_K8S_BINARY")
    if env_override:
        if shutil.which(env_override) is None:
            raise FileNotFoundError(
                f"BCL_K8S_BINARY={env_override!r} not found on PATH"
            )
        return env_override

    for candidate in ("oc", "kubectl"):
        if shutil.which(candidate):
            return candidate

    raise FileNotFoundError(
        "Neither 'oc' nor 'kubectl' found on PATH. Install one or set BCL_K8S_BINARY."
    )


# ─────────────────────────────────────────────────────────────────────────
# Result type
# ─────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class K8sResult:
    """Outcome of one shell invocation of `oc`/`kubectl`.

    All four shell-level facts are preserved so they can be audit-logged
    in full and inspected by humans later. Never lossy.
    """

    command: list[str]
    """The exact argv that ran. Useful for audit log + reproducing manually."""

    exit_code: int
    """0 = success per shell convention."""

    stdout: str
    """Captured stdout (UTF-8 decoded, may include warnings)."""

    stderr: str
    """Captured stderr (UTF-8 decoded). Where errors go even on success in OCP."""

    duration_seconds: float
    """Wall-clock duration of the invocation."""

    @property
    def ok(self) -> bool:
        return self.exit_code == 0

    def as_audit_payload(self) -> dict[str, Any]:
        """Audit-log-shaped dict. Truncates stdout/stderr to ~16KB each."""
        max_bytes = 16 * 1024
        return {
            "command": " ".join(self.command),
            "exit_code": self.exit_code,
            "duration_seconds": round(self.duration_seconds, 3),
            "stdout": self.stdout[:max_bytes],
            "stderr": self.stderr[:max_bytes],
            "stdout_truncated": len(self.stdout) > max_bytes,
            "stderr_truncated": len(self.stderr) > max_bytes,
        }


# ─────────────────────────────────────────────────────────────────────────
# Client
# ─────────────────────────────────────────────────────────────────────────


@dataclass
class K8sClient:
    """Thin async wrapper around `oc`/`kubectl`."""

    namespace: str
    binary: str = field(default_factory=_resolve_binary)
    default_timeout_seconds: float = 30.0

    async def _run(
        self,
        args: list[str],
        *,
        stdin: str | None = None,
        timeout: float | None = None,
    ) -> K8sResult:
        """Execute the binary with `args`. Optionally pipe `stdin` text in."""
        cmd = [self.binary, *args]
        eff_timeout = timeout if timeout is not None else self.default_timeout_seconds

        logger.debug("k8s exec: %s", " ".join(cmd))
        loop = asyncio.get_running_loop()
        t0 = loop.time()

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE if stdin is not None else None,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdin_bytes = stdin.encode("utf-8") if stdin is not None else None
            try:
                stdout_b, stderr_b = await asyncio.wait_for(
                    proc.communicate(input=stdin_bytes), timeout=eff_timeout
                )
            except asyncio.TimeoutError:
                # Kill the runaway process so we don't leak it
                proc.kill()
                await proc.wait()
                duration = loop.time() - t0
                return K8sResult(
                    command=cmd,
                    exit_code=-1,
                    stdout="",
                    stderr=f"timed out after {eff_timeout}s",
                    duration_seconds=duration,
                )
        except FileNotFoundError as e:
            duration = loop.time() - t0
            return K8sResult(
                command=cmd,
                exit_code=-1,
                stdout="",
                stderr=f"binary not found: {e}",
                duration_seconds=duration,
            )

        duration = loop.time() - t0
        return K8sResult(
            command=cmd,
            exit_code=proc.returncode if proc.returncode is not None else -1,
            stdout=stdout_b.decode("utf-8", errors="replace"),
            stderr=stderr_b.decode("utf-8", errors="replace"),
            duration_seconds=duration,
        )

    # ─── high-level operations ───────────────────────────────────────

    async def apply_yaml(
        self, yaml_text: str, *, timeout: float | None = None
    ) -> K8sResult:
        """`oc apply -n <ns> -f -` with the YAML piped on stdin.

        Idempotent: re-running creates-or-updates. Resources with our labels
        are managed; resources without our labels would be patched (we never
        emit a manifest without labels, so this is safe).
        """
        return await self._run(
            ["apply", "-n", self.namespace, "-f", "-"],
            stdin=yaml_text,
            timeout=timeout,
        )

    async def delete_resource(
        self,
        kind: str,
        name: str,
        *,
        ignore_not_found: bool = True,
        timeout: float | None = None,
    ) -> K8sResult:
        """`oc delete <kind>/<name> -n <ns> [--ignore-not-found]`."""
        args = ["delete", kind, name, "-n", self.namespace]
        if ignore_not_found:
            args.append("--ignore-not-found=true")
        return await self._run(args, timeout=timeout)

    async def get_deployment_status(
        self, name: str, *, timeout: float | None = None
    ) -> tuple[K8sResult, dict[str, Any] | None]:
        """Fetch Deployment status JSON. Returns (result, parsed_status_dict).

        parsed_status_dict is None if the resource doesn't exist or the
        response wasn't valid JSON.
        """
        result = await self._run(
            ["get", "deployment", name, "-n", self.namespace, "-o", "json"],
            timeout=timeout,
        )
        if not result.ok:
            return result, None
        try:
            parsed = json.loads(result.stdout)
            return result, parsed.get("status", {})
        except json.JSONDecodeError:
            return result, None

    async def wait_for_deployment_available(
        self,
        name: str,
        *,
        timeout_seconds: float = 300.0,
        poll_interval_seconds: float = 5.0,
    ) -> K8sResult:
        """Poll Deployment status until Available=True, or timeout.

        We use polling rather than `oc wait --for=condition=Available` because
        polling gives us per-check status we can stream into the audit log;
        `oc wait` is opaque on long waits.

        Returns the LAST K8sResult of the polling loop (Available or timeout).
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_seconds
        last_result: K8sResult | None = None

        while True:
            result, status = await self.get_deployment_status(name)
            last_result = result

            if status:
                conditions = status.get("conditions", []) or []
                available = next(
                    (c for c in conditions if c.get("type") == "Available"),
                    None,
                )
                if available and available.get("status") == "True":
                    return result

                # Surface progress info into the result's stderr for the
                # audit log if the deployment is still progressing.
                ready_replicas = status.get("readyReplicas", 0) or 0
                total_replicas = status.get("replicas", 0) or 0
                logger.debug(
                    "deployment %s waiting: ready=%d/%d",
                    name, ready_replicas, total_replicas,
                )

            if loop.time() >= deadline:
                # Compose a synthetic K8sResult for the timeout case so
                # callers can audit-log it uniformly.
                return K8sResult(
                    command=last_result.command if last_result else [self.binary, "wait", name],
                    exit_code=-1,
                    stdout=last_result.stdout if last_result else "",
                    stderr=(
                        (last_result.stderr if last_result else "")
                        + f"\ntimed out after {timeout_seconds}s waiting for "
                        f"Deployment/{name} Available=True"
                    ),
                    duration_seconds=timeout_seconds,
                )

            await asyncio.sleep(poll_interval_seconds)

    async def get_pod_name(
        self, deployment_name: str
    ) -> tuple[K8sResult, str | None]:
        """Find the (first) pod backing a Deployment via label selector.

        Returns (result, pod_name or None).
        """
        result = await self._run(
            [
                "get", "pod",
                "-n", self.namespace,
                "-l", f"app={deployment_name}",
                "-o", "jsonpath={.items[0].metadata.name}",
            ]
        )
        if result.ok and result.stdout.strip():
            return result, result.stdout.strip()
        return result, None


__all__ = ["K8sClient", "K8sResult"]
