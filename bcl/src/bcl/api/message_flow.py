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


# ─────────────────────────────────────────────────────────────────────────
# Queue-depth probe (uses MqClient to keep one subprocess pathway for MQSC)
# ─────────────────────────────────────────────────────────────────────────


_CURDEPTH_RE = re.compile(r"\bCURDEPTH\((\d+)\)")


async def _probe_queue_depth(
    client: MqClient,
    *,
    qm_name: str,
    pod_name: str,
    queue_name: str,
    namespace: str,
) -> tuple[int | None, str]:
    """DISPLAY QLOCAL(<q>) CURDEPTH; return (depth, raw_stdout)."""
    result = await client.apply_mqsc(
        qm_name=qm_name,
        pod_name=pod_name,
        mqsc_text=f"DISPLAY QLOCAL('{queue_name}') CURDEPTH",
        namespace=namespace,
        timeout=10.0,
    )
    if result.exit_code != 0:
        return None, result.raw_stdout + "\n" + result.raw_stderr
    m = _CURDEPTH_RE.search(result.raw_stdout)
    if not m:
        return None, result.raw_stdout
    return int(m.group(1)), result.raw_stdout


# ─────────────────────────────────────────────────────────────────────────
# amqsget stdout parsing
# ─────────────────────────────────────────────────────────────────────────


# amqsget formats each delivered message as: `message <PAYLOAD>`
# Capture whatever is between the angle brackets on a `message ...` line.
_AMQSGET_MESSAGE_RE = re.compile(r"^message\s+<(.*)>\s*$")


def _extract_payload_from_amqsget_stdout(stdout: str, expected: str) -> str | None:
    """Return the matched payload from amqsget output, or None.

    Strategy in order of preference:
      1. Find a `message <PAYLOAD>` line where PAYLOAD == expected.
      2. Fall back to the first `message <...>` line we see (useful for
         diagnosing: we got *a* message but it wasn't ours).
      3. Last-ditch substring scan in the raw stdout in case the format
         changes in a future MQ release.
    """
    first_seen: str | None = None
    for raw_line in stdout.splitlines():
        m = _AMQSGET_MESSAGE_RE.match(raw_line.strip())
        if not m:
            continue
        candidate = m.group(1)
        if candidate == expected:
            return candidate
        if first_seen is None:
            first_seen = candidate
    if expected in stdout:
        return expected
    return first_seen


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
    poll_started = datetime.now(UTC)
    poll_loop_t0 = asyncio.get_event_loop().time()
    deadline = poll_loop_t0 + min(15.0, body.timeout_seconds - put_duration)
    depth_seen: int | None = None
    poll_attempts = 0

    while asyncio.get_event_loop().time() < deadline:
        poll_attempts += 1
        depth, _raw = await _probe_queue_depth(
            mq_client,
            qm_name=flow.consumer_queue_manager,
            pod_name=consumer_qm_row.pod_name,
            queue_name=flow.consumer_queue_name,
            namespace=settings.namespace,
        )
        if depth is not None and depth >= 1:
            depth_seen = depth
            break
        await asyncio.sleep(0.5)

    poll_duration = asyncio.get_event_loop().time() - poll_loop_t0
    poll_ok = depth_seen is not None and depth_seen >= 1

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
        },
        response_payload={
            "depth_seen": depth_seen,
        },
        duration_ms=int(poll_duration * 1000),
        error_message=None if poll_ok else "message did not arrive within poll window",
    )
    last_lamport = audit_row.lamport_clock
    steps.append(MessageFlowStep(
        name="poll-consumer-queue-depth",
        started_at=poll_started,
        duration_seconds=poll_duration,
        success=poll_ok,
        detail=(
            f"polled {flow.consumer_queue_name}@{flow.consumer_queue_manager} "
            f"{poll_attempts}x; final depth={depth_seen}"
        ),
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

    # amqsget reads one or more messages from the named queue and
    # writes each as `message <PAYLOAD>` to stdout, then exits on its
    # own when the queue idles for ~15s (or on signal). It does not
    # read stdin, so we pass stdin=None which makes the helper omit
    # `-i` from `oc exec` and not open a stdin pipe at all.
    #
    # We give it ~10s of wall budget; amqsget will deliver our message
    # almost immediately because poll already confirmed depth>=1, then
    # we wait out its default idle window. The timeout_seconds floor
    # of 5.0 protects against poll having eaten too much of the budget.
    remaining_budget = body.timeout_seconds - put_duration - poll_duration
    get_timeout = max(5.0, min(20.0, remaining_budget))
    rc, get_stdout, get_stderr, get_duration = await _kubectl_exec_with_stdin(
        binary=mq_binary,
        namespace=settings.namespace,
        pod_name=consumer_qm_row.pod_name,
        argv=[AMQSGET_BIN, flow.consumer_queue_name, flow.consumer_queue_manager],
        stdin=None,
        timeout_seconds=get_timeout,
    )

    received = _extract_payload_from_amqsget_stdout(get_stdout, body.payload)
    # amqsget exits 0 even when timing out idle; we treat "got our payload"
    # as the real success signal, independent of exit code. If we got *some*
    # message but not ours, that's still useful diagnostic info but it's a
    # failure for this run.
    get_ok = (received == body.payload)

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
        },
        response_payload={
            "exit_code": rc,
            "stdout_tail": get_stdout[-1000:],
            "stderr_tail": get_stderr[-1000:],
            "payload_received": received,
        },
        duration_ms=int(get_duration * 1000),
        error_message=(
            None if get_ok
            else (get_stderr[:500] or "payload not found in amqsget stdout")
        ),
    )
    last_lamport = audit_row.lamport_clock
    steps.append(MessageFlowStep(
        name="amqsget",
        started_at=get_started,
        duration_seconds=get_duration,
        success=get_ok,
        detail=(
            f"GET from {flow.consumer_queue_name}; "
            f"payload {'matched' if received == body.payload else 'NOT matched'}"
        ),
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
