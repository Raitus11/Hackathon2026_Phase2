"""End-to-end message flow validation.

POST /topologies/{id}/test-message-flow

Given a producer app and a consumer app within a topology, this endpoint:
  1. Looks up a flow (CSV row) between them.
  2. Resolves the K8s pod for each side via the QueueManager rows.
  3. Inside the producer's pod, runs `amqsput` to PUT one message onto
     the producer-side queue (the QREMOTE for Remote flows, the QLOCAL
     for Local flows).
  4. Polls the consumer-side QM (or the same QM for Local flows) until
     the message is visible via `DISPLAY QLOCAL(...) CURDEPTH`.
  5. Runs `amqsget` inside the consumer's pod to GET one message.
  6. Returns timing, payload-match, and the Lamport range of every audit
     entry written along the way.

Why this matters: this is the "source topology faithfully reproduced"
gate from Beat 4 of the demo. If this passes after `realize-mq-objects`
on the source topology, we know the wiring works end-to-end and is
ready for migration.

Implementation notes:
  - `amqsput` and `amqsget` are sample applications shipped with every
    IBM MQ container. They take qm-name and queue-name as args and
    read/write one line of stdin/stdout per message.
  - We use absolute paths to /opt/mqm/samp/bin/{amqsput,amqsget} because
    these binaries are not on $PATH inside the IBM MQ container.
  - amqsput needs a blank line terminator on stdin to commit and exit.
    We send `payload + "\n\n"`.
  - amqsget does not read stdin at all; we pass `stdin=None` so the
    helper omits `-i` and does not open a stdin pipe.
  - We prefix bare pod names with `deployment/` so `oc exec` resolves
    to the running pod of the deployment without us having to look up
    the random pod-name suffix every call.
  - amqsget wraps each delivered message in angle brackets:
        message <PAYLOAD>
    We parse with a regex that matches that exact format.
  - We use the kubectl exec subprocess pattern (same as MqClient) for
    the same cross-platform reasons.
  - The full PUT/GET cycle is recorded as a sequence of audit entries
    sharing one correlation_id so the audit-log viewer can show "the
    proof message" as a contiguous block.
"""

from __future__ import annotations

import asyncio
import logging
import re
import subprocess
import uuid
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bcl.audit.lamport import LamportClock
from bcl.audit.writer import write_audit_entry
from bcl.config import get_settings
from bcl.db.session import get_session
from bcl.models.api import FlowSpec
from bcl.models.orm import (
    AuditOperation,
    QueueManager,
    Topology,
)
from bcl.provisioning.mq_client import MqClient

logger = logging.getLogger("bcl.api.message_flow")

router = APIRouter(prefix="/topologies", tags=["message-flow"])


# Absolute paths to the IBM MQ sample binaries inside the QM container.
# These binaries are not on $PATH; we invoke them directly to avoid
# `executable file not found in $PATH` errors from `oc exec`.
AMQSPUT_BIN = "/opt/mqm/samp/bin/amqsput"
AMQSGET_BIN = "/opt/mqm/samp/bin/amqsget"


# ─────────────────────────────────────────────────────────────────────────
# Request / response schemas
# ─────────────────────────────────────────────────────────────────────────


class TestMessageRequest(BaseModel):
    """Body for POST /topologies/{id}/test-message-flow."""

    producer_app_id: str = Field(
        min_length=1, max_length=64,
        description=(
            "The app_id whose pod will PUT the message. Must match a "
            "producer_app_id in one of the topology's flows."
        ),
    )
    consumer_app_id: str = Field(
        min_length=1, max_length=64,
        description=(
            "The app_id whose pod will GET the message. Must match a "
            "consumer_app_id paired with the producer above in some flow."
        ),
    )
    payload: str = Field(
        default="INTELLIAI-PROBE",
        min_length=1, max_length=512,
        description="Message payload. Echoed back from amqsget for match-check.",
    )
    timeout_seconds: float = Field(
        default=30.0, ge=1.0, le=120.0,
        description=(
            "Total budget for the round-trip. PUT, drain-wait, and GET "
            "share this budget."
        ),
    )


class MessageFlowStep(BaseModel):
    """One step in the message-flow trace. Surfaces in the response so
    the demo UI can show a step-by-step play of what happened."""

    name: str
    started_at: datetime
    duration_seconds: float
    success: bool
    detail: str
    audit_lamport: int | None = None
    """The Lamport timestamp of the audit row that recorded this step.
    None if the step didn't produce an audit row (e.g. local pre-checks)."""


class TestMessageResponse(BaseModel):
    """Response for the test-message endpoint."""

    correlation_id: str
    topology_id: int
    producer_app_id: str
    consumer_app_id: str

    flow_kind: Literal["Local", "Remote"]
    producer_qm: str
    consumer_qm: str
    producer_queue: str
    """Where the PUT lands — QREMOTE for remote, QLOCAL for local."""
    consumer_queue: str
    """Where the GET reads from — always a QLOCAL on the consumer side."""

    success: bool
    total_duration_seconds: float

    payload_sent: str
    payload_received: str | None
    payload_matches: bool

    steps: list[MessageFlowStep]
    audit_lamport_first: int | None
    audit_lamport_last: int | None


# ─────────────────────────────────────────────────────────────────────────
# Flow resolution
# ─────────────────────────────────────────────────────────────────────────


def _find_flow_between(
    flows: list[FlowSpec], producer_app_id: str, consumer_app_id: str
) -> FlowSpec | None:
    """Return the first flow between these two apps, or None.

    When both Local and Remote flows exist for the same pair we prefer
    Remote because it exercises XMITQ + channel transmission and is the
    more interesting validation. Local is fine as a fallback.
    """
    remote: FlowSpec | None = None
    local: FlowSpec | None = None
    for f in flows:
        if (
            f.producer_app_id == producer_app_id
            and f.consumer_app_id == consumer_app_id
        ):
            if f.flow_type == "Remote" and remote is None:
                remote = f
            elif f.flow_type == "Local" and local is None:
                local = f
    return remote or local


# ─────────────────────────────────────────────────────────────────────────
# kubectl exec subprocess helpers (same pattern as MqClient)
# ─────────────────────────────────────────────────────────────────────────


# A K8s pod created by a Deployment has the shape:
#   <deployment>-<10-char-replicaset-hash>-<5-char-pod-hash>
# e.g. "qm-wl6eebdj-775b69dbbc-wdxkz".  We use this regex to decide
# whether the stored name is a real pod or a deployment name, because
# `oc exec deployment/<pod-name>` fails with "deployments.apps not
# found" while `oc exec pod/<pod-name>` works.
_POD_NAME_SUFFIX_RE = re.compile(r"-[a-z0-9]{8,10}-[a-z0-9]{5}$")


def _resolve_pod_target(pod_name: str) -> str:
    """Return a target spec that `oc exec` accepts.

    Behaviour:
      - "pod/foo" or "deployment/foo" (already prefixed): returned as-is.
      - "qm-wl6eebdj-775b69dbbc-wdxkz" (full pod name with the K8s
        replicaset+pod hash suffix): returned as "pod/<name>".
      - "qm-wl6eebdj" (bare deployment name): returned as
        "deployment/<name>" so oc picks the running pod.

    The QueueManager.pod_name column currently stores **full pod names**
    (the value comes from `oc get pod -o name` in the provisioner), so
    in practice the pod/ branch fires. The deployment/ branch is kept
    for forward-compatibility if we ever switch the provisioner to
    record deployment names instead.

    Bug history: previous version unconditionally prefixed
    "deployment/", which produced
        "deployments.apps \"qm-wl6eebdj-775b69dbbc-wdxkz\" not found"
    because oc looked up a Deployment with the full pod name. Caught
    via the audit log after 7 hours of debugging on 2026-05-14.
    """
    if "/" in pod_name:
        return pod_name
    if _POD_NAME_SUFFIX_RE.search(pod_name):
        return f"pod/{pod_name}"
    return f"deployment/{pod_name}"


async def _kubectl_exec_with_stdin(
    *,
    binary: str,
    namespace: str,
    pod_name: str,
    argv: list[str],
    stdin: str | None,
    timeout_seconds: float,
) -> tuple[int, str, str, float]:
    """Run `oc exec [-i] -n NS POD -- ARGV...`; return (rc, stdout, stderr, duration).

    Mirrors MqClient._do_subprocess: synchronous subprocess.run via
    loop.run_in_executor so we don't trigger the Windows asyncio
    ProactorEventLoop requirement.

    `-i` is added only when stdin is provided (non-None). For commands
    like amqsget that read no stdin, omitting `-i` lets oc exec close
    the stdin pipe immediately and avoids spurious hangs on Windows
    where the empty-input case can leave the child waiting on a pipe
    that never sees EOF.
    """
    target = _resolve_pod_target(pod_name)
    cmd: list[str] = [binary, "exec"]
    if stdin is not None:
        cmd.append("-i")
    cmd += ["-n", namespace, target, "--", *argv]

    loop = asyncio.get_running_loop()
    t0 = loop.time()

    def _do() -> tuple[int, str, str]:
        try:
            proc = subprocess.run(
                cmd,
                input=stdin,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout_seconds,
                check=False,
            )
            return (proc.returncode, proc.stdout, proc.stderr)
        except subprocess.TimeoutExpired as e:
            # Surface any partial output the child produced before the kill.
            out = e.stdout if isinstance(e.stdout, str) else ""
            err = e.stderr if isinstance(e.stderr, str) else ""
            return (-1, out, (err + f"\ntimed out after {timeout_seconds}s").strip())
        except FileNotFoundError as e:
            return (-1, "", f"binary not found: {e}")
        except OSError as e:
            return (-1, "", f"OS error: {e}")

    rc, out, err = await loop.run_in_executor(None, _do)
    return rc, out, err, loop.time() - t0


async def _kubectl_exec_stream_until(
    *,
    binary: str,
    namespace: str,
    pod_name: str,
    argv: list[str],
    predicate,  # Callable[[str], bool]
    timeout_seconds: float,
) -> tuple[int, str, str, float]:
    """Run `oc exec ... -- ARGV` and stream stdout line-by-line. Terminate
    the child process as soon as `predicate(line)` returns True; otherwise
    bail out at `timeout_seconds` wall-time.

    Why this exists: subprocess.run captures stdout via a pipe which is
    fully buffered (not line-buffered) when stdout is not a TTY. amqsget
    writes "Sample AMQSGET0 start" and "message <PAYLOAD>" lines, but with
    a kernel pipe buffer they sit there until the process exits cleanly.
    If we SIGKILL the child for wall-time reasons, the buffer is dropped
    and we see empty stdout — even though amqsget already consumed the
    message (MQGMO_NO_SYNCPOINT autocommits per MQGET). Net result: queue
    drains, BCL reports "got nothing." This was the demo-day bug.

    Streaming reads each line as it appears and lets us terminate the
    child the moment we have what we need — turning a 4s wall-bound,
    empty-stdout failure into a ~200ms clean success.

    Returns (returncode, full_stdout_seen, stderr, duration). returncode
    is the actual process exit if it exited on its own (rare with kill);
    -2 when we killed it after match; -1 on timeout with no match.
    """
    target = _resolve_pod_target(pod_name)
    cmd: list[str] = [binary, "exec", "-n", namespace, target, "--", *argv]

    loop = asyncio.get_running_loop()
    t0 = loop.time()

    def _do() -> tuple[int, str, str]:
        # Use Popen with line-buffered text mode. bufsize=1 = line-buffer
        # on the Python side; the kernel pipe is still chunk-buffered but
        # amqsget itself line-buffers when stdout is a pipe to a TTY-like
        # process — `oc exec` allocates a PTY by default for interactive
        # use, so amqsget's printf calls flush per line.
        try:
            proc = subprocess.Popen(  # noqa: S603
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )
        except FileNotFoundError as e:
            return (-1, "", f"binary not found: {e}")
        except OSError as e:
            return (-1, "", f"OS error: {e}")

        collected_out: list[str] = []
        deadline_mono = t0 + timeout_seconds
        matched = False

        try:
            # Read stdout line by line. We cannot use select() on Windows
            # for pipes, so we rely on readline() with periodic checks
            # against the deadline. amqsget writes its lines promptly
            # under PTY allocation; readline returns each line as it
            # arrives.
            assert proc.stdout is not None
            while True:
                # Bail if deadline already passed before next readline
                # attempt — readline can block.
                if loop.time() >= deadline_mono:
                    break
                # readline() blocks; in practice amqsget produces output
                # within ~50ms of pod entry. If readline blocks past the
                # deadline, we kill the process below.
                line = proc.stdout.readline()
                if line == "":
                    # EOF — process exited on its own.
                    break
                collected_out.append(line)
                if predicate(line):
                    matched = True
                    break
        finally:
            if proc.poll() is None:
                # Process still running — terminate it. Use SIGTERM first
                # so amqsget can flush; if it lingers, SIGKILL.
                try:
                    proc.terminate()
                    try:
                        proc.wait(timeout=0.5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait(timeout=1.0)
                except OSError:
                    pass
            # Drain any remaining stdout (small amounts may have arrived
            # between match and terminate).
            if proc.stdout is not None:
                try:
                    rest = proc.stdout.read()
                    if rest:
                        collected_out.append(rest)
                except OSError:
                    pass
            stderr_text = ""
            if proc.stderr is not None:
                try:
                    stderr_text = proc.stderr.read() or ""
                except OSError:
                    pass

        if matched:
            rc = -2  # sentinel: we killed it after match
        elif proc.returncode is not None:
            rc = proc.returncode
        else:
            rc = -1  # timed out without a match

        return (rc, "".join(collected_out), stderr_text)

    rc, out, err = await loop.run_in_executor(None, _do)
    return rc, out, err, loop.time() - t0


# ─────────────────────────────────────────────────────────────────────────
# Queue-depth probe (uses MqClient to keep one subprocess pathway for MQSC)
# ─────────────────────────────────────────────────────────────────────────


_CURDEPTH_RE = re.compile(r"\bCURDEPTH\((\d+)\)")
# MQSC error patterns that we want to surface as a distinct error_kind
# in the diagnostic record. AMQ8147 = "MQSC object not found".
_AMQ_NOT_FOUND_RE = re.compile(r"\bAMQ8147\w*\b")


class _DepthProbe(BaseModel):
    """Structured result of one DISPLAY QLOCAL ... CURDEPTH probe.

    Replaces the previous (depth, raw_stdout) tuple, which silently
    swallowed three distinct failure modes:
      - queue doesn't exist on this QM (AMQ8147)
      - runmqsc itself failed (exit_code != 0)
      - runmqsc succeeded but output didn't match CURDEPTH regex
    Each is now a distinct error_kind so the audit response can tell
    the operator which one happened.
    """

    depth: int | None = None
    exit_code: int
    error_kind: Literal["ok", "queue_not_found", "mqsc_error", "no_curdepth_in_output"] = "ok"
    raw_stdout: str = ""
    raw_stderr: str = ""


async def _probe_queue_depth(
    client: MqClient,
    *,
    qm_name: str,
    pod_name: str,
    queue_name: str,
    namespace: str,
) -> _DepthProbe:
    """DISPLAY QLOCAL(<q>) CURDEPTH with structured error reporting.

    Returns a _DepthProbe whose error_kind distinguishes:
      - "ok" + depth=N: the queue exists and has N messages.
      - "queue_not_found": MQSC reported AMQ8147 (likely wrong queue
        name, wrong QM, or realize-mq-objects didn't run on this QM).
      - "mqsc_error": runmqsc itself failed (pod unreachable, QM not
        running, MQSC syntax error).
      - "no_curdepth_in_output": runmqsc succeeded but the output
        didn't contain CURDEPTH(...). Should not happen on a healthy
        QM; surface for forensics.
    """
    result = await client.apply_mqsc(
        qm_name=qm_name,
        pod_name=pod_name,
        mqsc_text=f"DISPLAY QLOCAL('{queue_name}') CURDEPTH",
        namespace=namespace,
        timeout=10.0,
    )
    combined_out = result.raw_stdout or ""
    combined_err = result.raw_stderr or ""

    if result.exit_code != 0:
        kind = (
            "queue_not_found"
            if _AMQ_NOT_FOUND_RE.search(combined_out + combined_err)
            else "mqsc_error"
        )
        return _DepthProbe(
            depth=None, exit_code=result.exit_code, error_kind=kind,
            raw_stdout=combined_out, raw_stderr=combined_err,
        )

    # Some MQSC builds return exit_code=0 but embed AMQ8147 in stdout
    # when the queue is missing (specifically when the DISPLAY targets
    # a non-existent object via wildcard). Catch that case.
    if _AMQ_NOT_FOUND_RE.search(combined_out):
        return _DepthProbe(
            depth=None, exit_code=0, error_kind="queue_not_found",
            raw_stdout=combined_out, raw_stderr=combined_err,
        )

    m = _CURDEPTH_RE.search(combined_out)
    if not m:
        return _DepthProbe(
            depth=None, exit_code=0, error_kind="no_curdepth_in_output",
            raw_stdout=combined_out, raw_stderr=combined_err,
        )
    return _DepthProbe(
        depth=int(m.group(1)), exit_code=0, error_kind="ok",
        raw_stdout=combined_out, raw_stderr=combined_err,
    )


async def _probe_xmitq_on_producer(
    client: MqClient,
    *,
    qm_name: str,
    pod_name: str,
    namespace: str,
) -> dict[str, int]:
    """Best-effort: list all XMITQs on the producer QM with CURDEPTH > 0.

    Called when poll-consumer-depth fails, to answer the diagnostic
    question: did the message ever leave the producer? If we see a
    nonzero XMITQ here, the message is stuck in transmission (channel
    not running, RCVR misconfigured, CHLAUTH blocking, etc).

    Returns {queue_name: depth} for every XMITQ with depth >= 1.
    Empty dict on probe failure (best-effort; don't fail the response
    just because forensics is unavailable).
    """
    try:
        result = await client.apply_mqsc(
            qm_name=qm_name,
            pod_name=pod_name,
            mqsc_text="DISPLAY QLOCAL(*) WHERE(USAGE EQ XMITQ) CURDEPTH",
            namespace=namespace,
            timeout=10.0,
        )
        if result.exit_code != 0:
            return {}
        out = result.raw_stdout or ""
        # MQSC output of DISPLAY QLOCAL(...) CURDEPTH looks like:
        #   AMQ8409I: Display Queue details.
        #      QUEUE(SYSTEM.CLUSTER.TRANSMIT.QUEUE)         TYPE(QLOCAL)
        #      CURDEPTH(0)                                  USAGE(XMITQ)
        # Parse by sliding QUEUE(<name>)...CURDEPTH(N) pairs.
        result_map: dict[str, int] = {}
        # Iterate blocks separated by QUEUE(...). Use findall on overlapping
        # pattern: queue name, then the nearest following CURDEPTH(N).
        pattern = re.compile(
            r"QUEUE\(([^)]+)\).*?CURDEPTH\((\d+)\)", re.DOTALL
        )
        for q, d in pattern.findall(out):
            depth = int(d)
            if depth >= 1:
                result_map[q] = depth
        return result_map
    except Exception:  # noqa: BLE001 — diagnostic only, never raise
        return {}


# ─────────────────────────────────────────────────────────────────────────
# amqsget stdout parsing
# ─────────────────────────────────────────────────────────────────────────


# amqsget formats each delivered message as: `message <PAYLOAD>`
# Capture whatever is between the angle brackets on a `message ...` line.
_AMQSGET_MESSAGE_RE = re.compile(r"^message\s+<(.*)>\s*$")


def _extract_strict_payload(stdout: str, expected: str) -> str | None:
    """Return the EXPECTED payload only if a `message <expected>` line
    is present in stdout. Returns None otherwise.

    Replaces the previous fuzzy extractor which would fall back to the
    "first message seen" and falsely report success when the queue had
    stale messages from prior probes. For a validation probe, anything
    other than an exact match is a failure we need to surface.
    """
    target_line = f"message <{expected}>"
    for raw_line in stdout.splitlines():
        if raw_line.strip() == target_line:
            return expected
        # Also accept the case where amqsget's output has trailing
        # whitespace before the close-bracket parse.
        m = _AMQSGET_MESSAGE_RE.match(raw_line.strip())
        if m and m.group(1) == expected:
            return expected
    return None


def _extract_first_message(stdout: str) -> str | None:
    """Return the first `message <...>` payload found in stdout, or
    None. Used purely for diagnostics: when we did NOT get the
    expected message, what DID we get? Helps the operator distinguish
    'queue was empty' from 'queue had a stale message'.
    """
    for raw_line in stdout.splitlines():
        m = _AMQSGET_MESSAGE_RE.match(raw_line.strip())
        if m:
            return m.group(1)
    return None


# Retained for backwards compatibility with anywhere else in the codebase
# that may import the old name. Internally just delegates to the strict
# extractor; falls back to first-message-seen only if no strict match.
def _extract_payload_from_amqsget_stdout(stdout: str, expected: str) -> str | None:
    """DEPRECATED: prefer _extract_strict_payload + _extract_first_message
    separately so the caller can distinguish 'got ours' from 'got somebody
    else's stale message'. Kept here so external importers don't break.
    """
    strict = _extract_strict_payload(stdout, expected)
    if strict is not None:
        return strict
    return _extract_first_message(stdout)


# ─────────────────────────────────────────────────────────────────────────
# Endpoint
# ─────────────────────────────────────────────────────────────────────────


@router.post(
    "/{topology_id}/test-message-flow",
    response_model=TestMessageResponse,
    summary="Send one message end-to-end through a topology to prove the wiring",
    description=(
        "PUTs one message via `amqsput` on the producer's pod and GETs "
        "it via `amqsget` on the consumer's pod, polling queue depth in "
        "between to confirm transmission. Returns a step-by-step trace + "
        "audit Lamport range.\n\n"
        "Pre-requisites:\n"
        "  - Both apps' QMs are provisioned (have pod_name + is_ready=true).\n"
        "  - The MQ objects are realized (POST /realize-mq-objects has run).\n\n"
        "Failure modes returned as 200 with `success=false` (not 4xx/5xx) so "
        "the UI can still render the step trace — the test failing is a "
        "valid datum, not a request error."
    ),
)
async def test_message_flow(
    topology_id: int,
    body: TestMessageRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> TestMessageResponse:
    settings = get_settings()
    correlation_id = str(uuid.uuid4())
    overall_t0 = datetime.now(UTC)
    overall_loop_t0 = asyncio.get_event_loop().time()
    steps: list[MessageFlowStep] = []
    first_lamport: int | None = None
    last_lamport: int | None = None

    # ── 1. Topology + flow resolution ────────────────────────────────
    topology = await session.get(Topology, topology_id)
    if topology is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Topology {topology_id} not found",
        )

    raw_flows = topology.spec.get("flows", [])
    flows = [FlowSpec.model_validate(f) for f in raw_flows]
    flow = _find_flow_between(flows, body.producer_app_id, body.consumer_app_id)
    if flow is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"No flow found between producer_app_id={body.producer_app_id!r} "
                f"and consumer_app_id={body.consumer_app_id!r} in topology "
                f"{topology_id}"
            ),
        )

    # ── 2. Pod resolution ────────────────────────────────────────────
    producer_qm_row = (await session.execute(
        select(QueueManager).where(
            QueueManager.topology_id == topology_id,
            QueueManager.qm_name == flow.producer_queue_manager,
        )
    )).scalar_one_or_none()
    consumer_qm_row = (await session.execute(
        select(QueueManager).where(
            QueueManager.topology_id == topology_id,
            QueueManager.qm_name == flow.consumer_queue_manager,
        )
    )).scalar_one_or_none()

    missing: list[str] = []
    if producer_qm_row is None or not producer_qm_row.pod_name:
        missing.append(f"producer QM {flow.producer_queue_manager}")
    if consumer_qm_row is None or not consumer_qm_row.pod_name:
        missing.append(f"consumer QM {flow.consumer_queue_manager}")
    if missing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"pods not available for: {missing}. Run /provision first.",
        )

    assert producer_qm_row is not None and producer_qm_row.pod_name is not None
    assert consumer_qm_row is not None and consumer_qm_row.pod_name is not None

    mq_binary = "oc"  # MqClient resolves it via shutil.which internally
    mq_client = MqClient(default_namespace=settings.namespace)

    # ── 3. PUT step ──────────────────────────────────────────────────
    put_started = datetime.now(UTC)
    put_loop_t0 = asyncio.get_event_loop().time()

    # amqsput reads payload from stdin and PUTs it onto the named queue.
    # Usage: amqsput <queue_name> [<qm_name>]
    # For Remote flows the producer-side queue is a QREMOTE; for Local
    # flows it's a QLOCAL. amqsput doesn't care — it just PUTs.
    #
    # amqsput reads ONE LINE per message and commits + exits on EOF
    # (when subprocess.run closes the stdin pipe). The previous
    # implementation sent `payload + "\n\n"`, which amqsput interpreted
    # as: line 1 = payload, line 2 = empty-string message, then EOF.
    # That put TWO messages on the queue per call: ours and an empty
    # one. The poll-depth check saw depth>=1 and passed, and FIFO
    # delivered ours first to amqsget, so the bug was masked — but the
    # empty message lingered on the queue and would be delivered to
    # whichever GET came next. Now we send exactly `payload + "\n"`:
    # one line, then EOF commits.
    rc, put_stdout, put_stderr, put_duration = await _kubectl_exec_with_stdin(
        binary=mq_binary,
        namespace=settings.namespace,
        pod_name=producer_qm_row.pod_name,
        argv=[AMQSPUT_BIN, flow.producer_queue_name, flow.producer_queue_manager],
        stdin=body.payload + "\n",
        timeout_seconds=min(10.0, body.timeout_seconds),
    )
    put_ok = (rc == 0)

    audit_row = await write_audit_entry(
        session,
        operation=AuditOperation.VALIDATION_RUN,
        success=put_ok,
        correlation_id=correlation_id,
        qm_name=flow.producer_queue_manager,
        app_id=body.producer_app_id,
        request_payload={
            "kind": "test_message_put",
            "queue": flow.producer_queue_name,
            "payload_bytes": len(body.payload),
        },
        response_payload={
            "exit_code": rc,
            "stdout_tail": put_stdout[-1000:],
            "stderr_tail": put_stderr[-1000:],
        },
        duration_ms=int(put_duration * 1000),
        error_message=None if put_ok else put_stderr[:500],
    )
    if first_lamport is None:
        first_lamport = audit_row.lamport_clock
    last_lamport = audit_row.lamport_clock
    steps.append(MessageFlowStep(
        name="amqsput",
        started_at=put_started,
        duration_seconds=put_duration,
        success=put_ok,
        detail=f"PUT {len(body.payload)}-byte payload onto {flow.producer_queue_name}",
        audit_lamport=audit_row.lamport_clock,
    ))

    if not put_ok:
        await session.commit()
        total_dur = asyncio.get_event_loop().time() - overall_loop_t0
        return TestMessageResponse(
            correlation_id=correlation_id, topology_id=topology_id,
            producer_app_id=body.producer_app_id,
            consumer_app_id=body.consumer_app_id,
            flow_kind=flow.flow_type, producer_qm=flow.producer_queue_manager,
            consumer_qm=flow.consumer_queue_manager,
            producer_queue=flow.producer_queue_name,
            consumer_queue=flow.consumer_queue_name,
            success=False, total_duration_seconds=total_dur,
            payload_sent=body.payload, payload_received=None,
            payload_matches=False, steps=steps,
            audit_lamport_first=first_lamport, audit_lamport_last=last_lamport,
        )

    # ── 4. Drain-wait: poll consumer-side queue depth ────────────────
    #
    # Local flows: message lands on consumer queue ~immediately. 15s is
    # generous.
    # Remote flows: message goes XMITQ -> SDR -> RCVR -> consumer QLOCAL.
    # Channel cold-start can take a few seconds; if SHORTRTY backoff is
    # active it can take a minute. We give Remote flows up to 25s here
    # (capped so we leave time for the GET step), and on timeout we
    # additionally probe XMITQs on the producer side to tell the
    # operator WHERE the message is stuck.
    poll_started = datetime.now(UTC)
    poll_loop_t0 = asyncio.get_event_loop().time()
    poll_budget_cap = 25.0 if flow.flow_type == "Remote" else 15.0
    # Reserve a minimum of 5s for the GET step.
    poll_budget = min(
        poll_budget_cap,
        max(2.0, body.timeout_seconds - put_duration - 5.0),
    )
    deadline = poll_loop_t0 + poll_budget
    depth_seen: int | None = None
    last_probe: _DepthProbe | None = None
    poll_attempts = 0

    while asyncio.get_event_loop().time() < deadline:
        poll_attempts += 1
        probe = await _probe_queue_depth(
            mq_client,
            qm_name=flow.consumer_queue_manager,
            pod_name=consumer_qm_row.pod_name,
            queue_name=flow.consumer_queue_name,
            namespace=settings.namespace,
        )
        last_probe = probe
        if probe.error_kind == "queue_not_found":
            # No point polling further — the queue genuinely isn't there.
            # Fail fast with diagnostic info.
            break
        if probe.depth is not None and probe.depth >= 1:
            depth_seen = probe.depth
            break
        await asyncio.sleep(0.5)

    poll_duration = asyncio.get_event_loop().time() - poll_loop_t0
    poll_ok = depth_seen is not None and depth_seen >= 1

    # Forensics on failure: where's the message?
    stuck_xmitqs: dict[str, int] = {}
    poll_error_kind: str | None = None
    if not poll_ok:
        if last_probe is not None:
            poll_error_kind = last_probe.error_kind
        if flow.flow_type == "Remote":
            stuck_xmitqs = await _probe_xmitq_on_producer(
                mq_client,
                qm_name=flow.producer_queue_manager,
                pod_name=producer_qm_row.pod_name,
                namespace=settings.namespace,
            )

    # Build a human-readable failure summary that names the actual fault
    # (not just "didn't arrive"). This is what the operator sees in the
    # audit response and in the UI step trace.
    if poll_ok:
        poll_detail = (
            f"polled {flow.consumer_queue_name}@{flow.consumer_queue_manager} "
            f"{poll_attempts}x; final depth={depth_seen}"
        )
        poll_err_msg = None
    elif poll_error_kind == "queue_not_found":
        poll_detail = (
            f"consumer queue {flow.consumer_queue_name!r} NOT FOUND on QM "
            f"{flow.consumer_queue_manager!r}. Did /realize-mq-objects run on "
            f"this QM? Check that consumer_queue_name in the flow row matches "
            f"the QLOCAL actually defined."
        )
        poll_err_msg = "consumer queue does not exist (AMQ8147)"
    elif poll_error_kind == "mqsc_error":
        poll_detail = (
            f"MQSC probe failed on {flow.consumer_queue_manager} pod "
            f"{consumer_qm_row.pod_name}: exit_code="
            f"{last_probe.exit_code if last_probe else '?'}. "
            f"stderr: {(last_probe.raw_stderr if last_probe else '')[:200]}"
        )
        poll_err_msg = "MQSC probe failed; consumer QM may be unreachable"
    elif stuck_xmitqs:
        # The message left amqsput but never crossed the channel.
        stuck_summary = ", ".join(f"{q}={d}" for q, d in stuck_xmitqs.items())
        poll_detail = (
            f"message stuck in transmission on producer QM "
            f"{flow.producer_queue_manager}. XMITQ depths: {stuck_summary}. "
            f"Check CHSTATUS of SDR channel to {flow.consumer_queue_manager}; "
            f"likely INACTIVE, RETRYING, or BLOCKED by CHLAUTH on consumer."
        )
        poll_err_msg = (
            f"message stuck in XMITQ ({stuck_summary}); channel likely not running"
        )
    else:
        poll_detail = (
            f"polled {flow.consumer_queue_name}@{flow.consumer_queue_manager} "
            f"{poll_attempts}x in {poll_duration:.1f}s; queue stayed empty and "
            f"no producer-side XMITQ has the message either. Possible causes: "
            f"realize-mq-objects mis-wired the QREMOTE (wrong RNAME/RQMNAME), "
            f"or another consumer drained the queue between PUT and poll."
        )
        poll_err_msg = "message did not arrive within poll window (root cause unclear)"

    audit_row = await write_audit_entry(
        session,
        operation=AuditOperation.VALIDATION_RUN,
        success=poll_ok,
        correlation_id=correlation_id,
        qm_name=flow.consumer_queue_manager,
        app_id=body.consumer_app_id,
        request_payload={
            "kind": "test_message_poll_depth",
            "queue": flow.consumer_queue_name,
            "poll_attempts": poll_attempts,
            "flow_kind": flow.flow_type,
            "poll_budget_seconds": poll_budget,
        },
        response_payload={
            "depth_seen": depth_seen,
            "poll_error_kind": poll_error_kind,
            "stuck_xmitqs": stuck_xmitqs or None,
            "last_probe_stdout_tail": (
                (last_probe.raw_stdout[-500:] if last_probe else "")
            ),
            "last_probe_stderr_tail": (
                (last_probe.raw_stderr[-500:] if last_probe else "")
            ),
        },
        duration_ms=int(poll_duration * 1000),
        error_message=poll_err_msg,
    )
    last_lamport = audit_row.lamport_clock
    steps.append(MessageFlowStep(
        name="poll-consumer-queue-depth",
        started_at=poll_started,
        duration_seconds=poll_duration,
        success=poll_ok,
        detail=poll_detail,
        audit_lamport=audit_row.lamport_clock,
    ))

    if not poll_ok:
        await session.commit()
        total_dur = asyncio.get_event_loop().time() - overall_loop_t0
        return TestMessageResponse(
            correlation_id=correlation_id, topology_id=topology_id,
            producer_app_id=body.producer_app_id,
            consumer_app_id=body.consumer_app_id,
            flow_kind=flow.flow_type, producer_qm=flow.producer_queue_manager,
            consumer_qm=flow.consumer_queue_manager,
            producer_queue=flow.producer_queue_name,
            consumer_queue=flow.consumer_queue_name,
            success=False, total_duration_seconds=total_dur,
            payload_sent=body.payload, payload_received=None,
            payload_matches=False, steps=steps,
            audit_lamport_first=first_lamport, audit_lamport_last=last_lamport,
        )

    # ── 5. GET step ──────────────────────────────────────────────────
    get_started = datetime.now(UTC)
    get_loop_t0 = asyncio.get_event_loop().time()

    # amqsget reads messages from the queue using MQGET with WAIT
    # (WaitInterval=15000ms) and writes each as `message <PAYLOAD>` to
    # stdout. There is no `-w` flag on the shipped sample.
    #
    # CRITICAL: oc exec captures stdout via a pipe; subprocess.run with
    # capture_output=True fully buffers that pipe. amqsget writes the
    # message line, MQGET commits it (MQGMO_NO_SYNCPOINT), then sits
    # idle on the next MQGET — but the line we want is stuck in the
    # kernel buffer. If we SIGKILL on wall-time, buffer is dropped and
    # we see empty stdout while the queue is drained. This was the
    # demo-day bug.
    #
    # Fix: stream stdout line-by-line and terminate amqsget the moment
    # we see "message <PAYLOAD>". Total wall time goes from 4s-with-
    # empty-output to ~200ms-with-the-payload.
    expected_marker = f"message <{body.payload}>"
    expected_marker_alt = f"message <{body.payload}>"  # exact match used by predicate
    
    def _line_has_our_payload(line: str) -> bool:
        return expected_marker_alt in line

    remaining_budget = body.timeout_seconds - put_duration - poll_duration
    # Keep a generous ceiling: in pathological cases amqsget takes a few
    # seconds to start (cold MQI connect, security cache miss). 10s is
    # plenty given a healthy QM produces output in <100ms.
    get_timeout = max(2.0, min(10.0, remaining_budget))
    rc, get_stdout, get_stderr, get_duration = await _kubectl_exec_stream_until(
        binary=mq_binary,
        namespace=settings.namespace,
        pod_name=consumer_qm_row.pod_name,
        argv=[AMQSGET_BIN, flow.consumer_queue_name, flow.consumer_queue_manager],
        predicate=_line_has_our_payload,
        timeout_seconds=get_timeout,
    )

    # Strict matching: we accept the message ONLY if amqsget's stdout
    # contains a `message <PAYLOAD>` line with PAYLOAD exactly equal to
    # body.payload. If we got a different message (stale entry from a
    # prior run, or someone else's traffic), surface it as a diagnostic
    # but mark this run as failed.
    received = _extract_strict_payload(get_stdout, body.payload)
    other_message_seen = _extract_first_message(get_stdout)
    get_ok = (received == body.payload)

    # Build a sharp failure detail when we missed.
    if get_ok:
        get_detail = (
            f"GET from {flow.consumer_queue_name}; payload matched "
            f"({len(body.payload)} bytes) in {get_duration*1000:.0f}ms"
        )
        get_err_msg = None
    elif other_message_seen is not None and other_message_seen != body.payload:
        get_detail = (
            f"GET from {flow.consumer_queue_name}; got A message but it was "
            f"NOT ours. Expected {body.payload!r}, got {other_message_seen!r}. "
            f"Likely a stale message from a prior probe — CLEAR the queue and "
            f"retry. (amqsget exit_code={rc})"
        )
        get_err_msg = (
            f"wrong message received (expected {body.payload!r}, got "
            f"{other_message_seen!r})"
        )
    elif rc == -1:
        get_detail = (
            f"GET from {flow.consumer_queue_name}; amqsget timed out at "
            f"{get_timeout:.1f}s without producing our payload. "
            f"Possible causes: another consumer drained the queue between "
            f"poll and GET, or amqsget could not authenticate (check MCAUSER "
            f"on the consumer QM). stderr: {get_stderr[:300]}"
        )
        get_err_msg = "amqsget produced no matching output within wall-time bound"
    else:
        get_detail = (
            f"GET from {flow.consumer_queue_name}; amqsget exit_code={rc} but "
            f"no parseable `message <...>` line in stdout. "
            f"stdout tail: {get_stdout[-200:]!r}"
        )
        get_err_msg = get_stderr[:500] or "no message line in amqsget stdout"

    audit_row = await write_audit_entry(
        session,
        operation=AuditOperation.VALIDATION_RUN,
        success=get_ok,
        correlation_id=correlation_id,
        qm_name=flow.consumer_queue_manager,
        app_id=body.consumer_app_id,
        request_payload={
            "kind": "test_message_get",
            "queue": flow.consumer_queue_name,
            "wall_timeout_seconds": get_timeout,
        },
        response_payload={
            "exit_code": rc,
            "stdout_tail": get_stdout[-1000:],
            "stderr_tail": get_stderr[-1000:],
            "payload_received": received,
            "other_message_seen": (
                other_message_seen if other_message_seen != received else None
            ),
        },
        duration_ms=int(get_duration * 1000),
        error_message=get_err_msg,
    )
    last_lamport = audit_row.lamport_clock
    steps.append(MessageFlowStep(
        name="amqsget",
        started_at=get_started,
        duration_seconds=get_duration,
        success=get_ok,
        detail=get_detail,
        audit_lamport=audit_row.lamport_clock,
    ))

    await session.commit()
    total_dur = asyncio.get_event_loop().time() - overall_loop_t0

    return TestMessageResponse(
        correlation_id=correlation_id, topology_id=topology_id,
        producer_app_id=body.producer_app_id,
        consumer_app_id=body.consumer_app_id,
        flow_kind=flow.flow_type, producer_qm=flow.producer_queue_manager,
        consumer_qm=flow.consumer_queue_manager,
        producer_queue=flow.producer_queue_name,
        consumer_queue=flow.consumer_queue_name,
        success=get_ok and put_ok and poll_ok,
        total_duration_seconds=total_dur,
        payload_sent=body.payload,
        payload_received=received,
        payload_matches=(received == body.payload),
        steps=steps,
        audit_lamport_first=first_lamport,
        audit_lamport_last=last_lamport,
    )


__all__ = ["router"]
