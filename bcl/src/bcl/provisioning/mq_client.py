"""
MQ administrative client.

Speaks to a queue manager inside a Kubernetes pod by shelling out to
`oc exec -i ... runmqsc`. Pipes the MQSC text in via stdin, captures
stdout, parses it into a structured MqscResult.

Cross-platform note: uses subprocess.run inside loop.run_in_executor
(same pattern as K8sClient). Avoids the asyncio.create_subprocess_exec
NotImplementedError on Windows + uvicorn SelectorEventLoop.

Single-source-of-truth for MQSC apply operations. Every call is logged
to the audit log by the caller; this module does not write to the DB.

Author note: this is the Layer 2 primitive (MQ object provisioning).
Layer 1 is the K8sClient (PVC/Secret/Deployment/Service provisioning).
Distinct lifecycles, distinct audit operations, both Lamport-clocked.
"""

from __future__ import annotations

import asyncio
import logging
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import Literal

logger = logging.getLogger(__name__)


# ─────────────────────── Output types ───────────────────────

# Severity inferred from the trailing letter of an AMQ code:
#   AMQ8006I → informational (success)
#   AMQ8405W → warning
#   AMQ8147E → error
Severity = Literal["I", "W", "E"]


@dataclass
class MqscCommandOutcome:
    """Parsed outcome of a single MQSC command inside a batch."""

    line_number: int
    """The '1 :', '2 :' etc. prefix runmqsc echoes back."""

    command_text: str
    """The command text as runmqsc echoed it (may be reformatted)."""

    amq_code: str | None
    """E.g. 'AMQ8006I' for success, 'AMQ8350E' for queue-exists, etc.
    None if no AMQ code was emitted (unusual)."""

    severity: Severity
    """'I' informational, 'W' warning, 'E' error. Derived from amq_code suffix
    or defaults to 'E' if the command has any 'Syntax error' marker."""

    detail: str
    """Human-readable explanation from runmqsc."""

    @property
    def success(self) -> bool:
        return self.severity == "I"


@dataclass
class MqscResult:
    """Structured result of one MqClient.apply_mqsc invocation."""

    exit_code: int
    raw_stdout: str
    raw_stderr: str
    duration_seconds: float

    # Parsed summary block
    commands_read: int
    commands_processed: int
    syntax_errors: int
    not_processed: int

    # Per-command outcomes
    per_command: list[MqscCommandOutcome] = field(default_factory=list)

    @property
    def all_succeeded(self) -> bool:
        """True iff every command was processed without warnings or errors."""
        return (
            self.exit_code == 0
            and self.syntax_errors == 0
            and self.not_processed == 0
            and all(c.success for c in self.per_command)
        )

    @property
    def has_failures(self) -> bool:
        return (
            self.exit_code != 0
            or self.syntax_errors > 0
            or self.not_processed > 0
            or any(c.severity == "E" for c in self.per_command)
        )

    def summary(self) -> str:
        """One-line human summary for logs."""
        return (
            f"exit={self.exit_code} "
            f"read={self.commands_read} "
            f"processed={self.commands_processed} "
            f"syntax_errors={self.syntax_errors} "
            f"not_processed={self.not_processed} "
            f"duration={self.duration_seconds:.2f}s"
        )


# ─────────────────────── Parser ───────────────────────

# A command-echo line:  "     1 : DEFINE QLOCAL('TEST.HELLO') REPLACE"
_LINE_HEADER = re.compile(r"^\s*(\d+)\s*:\s*(.*)$")

# An AMQ message:  "AMQ8006I: IBM MQ queue created."
# Also tolerates the form "AMQ8006I IBM MQ queue created." with no colon.
_AMQ_LINE = re.compile(r"^\s*(AMQ\d{4})([IWE])[:\s](.*)$")

# Summary block lines. runmqsc localizes some words to "One", "No" instead
# of using digits, so we tolerate either.
_SUM_READ = re.compile(
    r"^(\d+|One|No)\s+MQSC commands?\s+read\.?$", re.IGNORECASE
)
_SUM_SYNTAX_ZERO = re.compile(
    r"^No\s+commands?\s+(have\s+)?(a\s+)?syntax errors?\.?$", re.IGNORECASE
)
_SUM_SYNTAX_NONZERO = re.compile(
    r"^(\d+|One)\s+commands?\s+(have|has)\s+(a\s+)?syntax errors?\.?$",
    re.IGNORECASE,
)
_SUM_PROCESSED_ALL = re.compile(
    r"^All valid MQSC commands were processed\.?$", re.IGNORECASE
)
_SUM_PROCESSED_COUNT = re.compile(
    r"^(\d+|One|No)\s+commands?\s+(were\s+)?processed\.?$", re.IGNORECASE
)
_SUM_NOT_PROCESSED = re.compile(
    r"^(\d+|One|No)\s+commands?\s+could not be processed\.?$", re.IGNORECASE
)


def _word_to_int(s: str) -> int:
    """runmqsc says 'One' for 1 and 'No' for 0 in some summaries."""
    lower = s.lower()
    if lower == "one":
        return 1
    if lower == "no":
        return 0
    return int(s)


def parse_runmqsc_output(
    stdout: str,
    *,
    expected_commands: int | None = None,
) -> tuple[
    int, int, int, int, list[MqscCommandOutcome]
]:
    """Parse runmqsc stdout into (commands_read, commands_processed,
    syntax_errors, not_processed, per_command_outcomes).

    The parser is robust to runmqsc's variable formatting:
    - Numbers may be digits or words ("One", "No")
    - Summary phrases vary slightly across MQ versions
    - Command echoes may be indented inconsistently
    - "All valid MQSC commands were processed." is used in place of a count.

    Args:
        stdout: full stdout text from runmqsc
        expected_commands: if provided, used as fallback for commands_processed
                           when runmqsc says "All valid MQSC commands were processed"

    Returns:
        Tuple of (read, processed, syntax_errors, not_processed, per_command).
    """
    lines = stdout.splitlines()

    # State machine: walk lines, track current command number, collect AMQ
    # messages that follow each command line.
    per_command: list[MqscCommandOutcome] = []
    current_line_num: int | None = None
    current_command: str = ""
    current_amq: tuple[str, Severity, str] | None = None
    current_had_syntax_error = False

    def _flush() -> None:
        """Push the current command's parsed outcome (if any)."""
        nonlocal current_line_num, current_command, current_amq, current_had_syntax_error
        if current_line_num is None:
            return
        if current_amq is not None:
            code, sev, detail = current_amq
        elif current_had_syntax_error:
            code, sev, detail = (None, "E", "Syntax error detected.")
        else:
            # No AMQ code, no syntax error → assume success (some commands
            # emit no explicit message). Default to "I".
            code, sev, detail = (None, "I", "")
        per_command.append(
            MqscCommandOutcome(
                line_number=current_line_num,
                command_text=current_command.strip(),
                amq_code=code,
                severity=sev,
                detail=detail.strip(),
            )
        )
        current_line_num = None
        current_command = ""
        current_amq = None
        current_had_syntax_error = False

    commands_read = 0
    commands_processed = 0
    syntax_errors = 0
    not_processed = 0
    saw_processed_summary = False

    for raw in lines:
        line = raw.rstrip()
        if not line.strip():
            continue

        # Summary lines come last; check them with priority since they
        # match similar shapes to command output.
        if _SUM_PROCESSED_ALL.match(line.strip()):
            saw_processed_summary = True
            # Defer assigning commands_processed until we know commands_read.
            continue
        m = _SUM_NOT_PROCESSED.match(line.strip())
        if m:
            not_processed = _word_to_int(m.group(1))
            continue
        m = _SUM_PROCESSED_COUNT.match(line.strip())
        if m:
            commands_processed = _word_to_int(m.group(1))
            saw_processed_summary = True
            continue
        m = _SUM_SYNTAX_NONZERO.match(line.strip())
        if m:
            syntax_errors = _word_to_int(m.group(1))
            continue
        if _SUM_SYNTAX_ZERO.match(line.strip()):
            syntax_errors = 0
            continue
        m = _SUM_READ.match(line.strip())
        if m:
            commands_read = _word_to_int(m.group(1))
            continue

        # Command echo:  "     1 : DEFINE ..."
        m = _LINE_HEADER.match(line)
        if m and not line.strip().startswith("AMQ"):
            # Starting a new command — flush previous.
            _flush()
            current_line_num = int(m.group(1))
            current_command = m.group(2)
            continue

        # AMQ message — attach to current command.
        m = _AMQ_LINE.match(line)
        if m:
            code_base, sev_letter, detail = m.group(1), m.group(2), m.group(3)
            full_code = f"{code_base}{sev_letter}"
            sev: Severity = sev_letter  # type: ignore[assignment]
            # If multiple AMQs follow one command, prefer the most severe.
            if current_amq is None or _is_more_severe(sev, current_amq[1]):
                current_amq = (full_code, sev, detail)
            continue

        # "Syntax error detected at or near..."  — these don't have an AMQ code.
        if "syntax error" in line.lower():
            current_had_syntax_error = True
            continue

        # Otherwise skip — could be queue-attribute output from DISPLAY,
        # banner text, blank lines, etc. The parser doesn't need to model it.

    # Flush the final command
    _flush()

    # Reconcile "All valid MQSC commands were processed" with read count.
    if saw_processed_summary and commands_processed == 0 and commands_read > 0:
        commands_processed = commands_read - syntax_errors

    return commands_read, commands_processed, syntax_errors, not_processed, per_command


def _is_more_severe(a: Severity, b: Severity) -> bool:
    """Order: E > W > I."""
    rank = {"I": 0, "W": 1, "E": 2}
    return rank[a] > rank[b]


# ─────────────────────── Client ───────────────────────


class MqClient:
    """
    Talks to a queue manager inside a Kubernetes pod via `oc exec -i`.

    Stateless. One instance can serve many QMs/pods. Each apply_mqsc call
    spawns one `oc exec` subprocess.

    The caller is responsible for:
    - Resolving qm_name to pod_name (typically via k8s labels)
    - Writing audit log entries before and after the call
    - Choosing the namespace (defaults to env-or-config)
    """

    def __init__(
        self,
        *,
        binary: str = "oc",
        default_namespace: str = "roco-dev",
        default_timeout_seconds: float = 60.0,
    ) -> None:
        self.binary = self._resolve_binary(binary)
        self.default_namespace = default_namespace
        self.default_timeout_seconds = default_timeout_seconds

    @staticmethod
    def _resolve_binary(name: str) -> str:
        resolved = shutil.which(name)
        return resolved if resolved else name

    async def apply_mqsc(
        self,
        *,
        qm_name: str,
        pod_name: str,
        mqsc_text: str,
        namespace: str | None = None,
        timeout: float | None = None,
    ) -> MqscResult:
        """Apply a (possibly multi-line) MQSC batch against `qm_name` in `pod_name`.

        Args:
            qm_name: MQ queue manager name (e.g. "SRC_QM_CB_QM")
            pod_name: Kubernetes pod (e.g. "qm-src-qm-cb-67d4c5cf79-92fzk")
            mqsc_text: one or more MQSC commands, newline-separated. May or
                       may not end with `END` — we add it implicitly via
                       runmqsc's stdin-close semantics.
            namespace: K8s namespace; defaults to constructor default
            timeout: subprocess timeout in seconds

        Returns:
            MqscResult with parsed counters and per-command outcomes.
        """
        ns = namespace or self.default_namespace
        eff_timeout = timeout if timeout is not None else self.default_timeout_seconds

        cmd = [
            self.binary,
            "exec",
            "-i",
            "-n",
            ns,
            pod_name,
            "--",
            "runmqsc",
            qm_name,
        ]

        logger.debug("mq apply_mqsc: pod=%s qm=%s bytes=%d", pod_name, qm_name, len(mqsc_text))

        loop = asyncio.get_running_loop()
        t0 = loop.time()

        # Count input lines for parser fallback (count non-empty lines).
        expected = sum(1 for line in mqsc_text.splitlines() if line.strip())

        def _do_subprocess() -> tuple[int, str, str]:
            try:
                proc = subprocess.run(
                    cmd,
                    input=mqsc_text,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=eff_timeout,
                    check=False,
                )
                return (proc.returncode, proc.stdout, proc.stderr)
            except subprocess.TimeoutExpired:
                return (-1, "", f"timed out after {eff_timeout}s")
            except FileNotFoundError as e:
                return (-1, "", f"binary not found: {e}")
            except OSError as e:
                return (-1, "", f"OS error: {e}")

        exit_code, stdout, stderr = await loop.run_in_executor(None, _do_subprocess)
        duration = loop.time() - t0

        read, processed, syntax_errs, not_proc, per_cmd = parse_runmqsc_output(
            stdout, expected_commands=expected
        )

        result = MqscResult(
            exit_code=exit_code,
            raw_stdout=stdout,
            raw_stderr=stderr,
            duration_seconds=duration,
            commands_read=read,
            commands_processed=processed,
            syntax_errors=syntax_errs,
            not_processed=not_proc,
            per_command=per_cmd,
        )

        logger.info("mq apply_mqsc done: %s", result.summary())
        return result
