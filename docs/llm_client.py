"""LLM client — provider-agnostic facade over Tachyon / stub.

The BCL talks to language models through exactly this module. Agents
import `complete_structured` (or `complete_text` for free-form output)
and never reach for HTTP directly.

Backends:

  - tachyon: Wells Fargo's internal LLM gateway exposing Gemini 2.5
             Pro and friends. Used on office laptops where the network
             reaches the Tachyon Apigee gateway. Reached through the
             `tachyon-langchain-client` package.
  - stub:    a deterministic in-process echo backend. Returns a
             constant JSON shape. Used in tests and when the operator
             explicitly wants the system to NEVER call out to a model
             (BCL_LLM_PROVIDER=stub). The Migration Planner's
             deterministic fallback is NOT this backend — it is a
             separate, planner-specific Python function with domain
             knowledge. The stub is a generic last-resort.

Design notes:

  - One outbound HTTP call per `complete_*` invocation. No streaming
    yet (chat will add it as a separate path).
  - Structured output is enforced by Pydantic validation in the caller.
    We do NOT trust the model to obey JSON schemas; we ask, parse,
    and if parsing fails the caller catches ValidationError and
    falls back. See Schluntz & Zhang, "Building effective agents"
    (Anthropic 2024) — start with the simplest possible agent and
    only add orchestration when measurable benefits demand it.
  - Timeouts are caller-tunable but default to BCL_LLM_REQUEST_TIMEOUT_SECONDS.
  - The client returns a structured LLMResponse so the agent's
    AgentInvocation audit row can record tokens, duration, model.

References:

  - Gemini structured output: https://ai.google.dev/gemini-api/docs/structured-output
  - Anthropic, "Building effective agents" (2024-12).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Literal

import httpx

from bcl.config import Settings, get_settings

logger = logging.getLogger("bcl.llm.client")


# ─────────────────────────────────────────────────────────────────────────
# Tachyon client — lazy module-global
# ─────────────────────────────────────────────────────────────────────────
#
# Tachyon is reached through Wells Fargo's `tachyon-langchain-client`
# package, NOT a hand-rolled REST call. The package owns the Apigee
# OAuth handshake (CONSUMER_KEY/SECRET -> APIGEE_URL -> bearer token),
# the corporate-CA TLS bundle (CERTS_PATH), and the gateway headers.
# It reads all of that from os.environ — the same variable names Phase 1
# used (API_KEY, CONSUMER_KEY, CONSUMER_SECRET, APIGEE_URL, BASE_URL,
# CERTS_PATH, USE_CASE_ID). It is the proven path; a raw httpx POST is
# not, because Tachyon is not a plain OpenAI-compatible endpoint.
#
# The client is constructed once and cached. Construction is cheap but
# not free (it may mint an Apigee token), so we lazy-init on first use.

_tachyon_client: Any = None
_tachyon_client_failed: bool = False
"""Set True once construction has failed, so we don't retry the import/
init on every call — a missing package or bad creds won't recover within
a process lifetime, and the caller's deterministic fallback handles it."""


# ─────────────────────────────────────────────────────────────────────────
# Response shape
# ─────────────────────────────────────────────────────────────────────────


@dataclass
class LLMResponse:
    """One LLM completion result.

    `model` is the fully-qualified model identifier including the
    backend prefix (e.g. 'tachyon:gemini-2.5-pro'). This goes
    straight into AgentInvocation.model so the audit log can answer
    "which model said what" without separate joins.
    """

    text: str
    """The model's textual output. For structured-output requests
    the caller must json.loads this; the client does not parse."""

    model: str
    """Backend-prefixed model identifier — e.g. 'tachyon:gemini-2.5-pro'."""

    tokens_in: int | None
    tokens_out: int | None
    duration_ms: int

    backend: Literal["tachyon", "stub"]
    """Which backend served this. Surfaces in audit log."""

    raw_response: dict[str, Any] | None = None
    """Backend-native response envelope. Kept for forensics."""


# ─────────────────────────────────────────────────────────────────────────
# Errors
# ─────────────────────────────────────────────────────────────────────────


class LLMError(RuntimeError):
    """Base for all LLM-client errors. Callers catch this for fallback."""


class LLMTimeoutError(LLMError):
    """The request exceeded the per-call timeout."""


class LLMProviderError(LLMError):
    """The provider returned a non-2xx response. Includes status and body."""

    def __init__(self, status_code: int, body: str) -> None:
        super().__init__(f"provider returned HTTP {status_code}: {body[:500]}")
        self.status_code = status_code
        self.body = body


class LLMConfigError(LLMError):
    """The selected provider is misconfigured (missing key/endpoint)."""


# ─────────────────────────────────────────────────────────────────────────
# Public API — text completion
# ─────────────────────────────────────────────────────────────────────────


async def complete_text(
    *,
    system_prompt: str,
    user_prompt: str,
    settings: Settings | None = None,
    timeout_seconds: float | None = None,
    max_tokens: int | None = None,
    temperature: float | None = None,
) -> LLMResponse:
    """Plain-text completion. The model's output is returned verbatim.

    Used by:
        - Operator Assistant (free-form chat answers)
        - Compliance Narrator (markdown narrative)
        - Anything where the consumer parses the text downstream.

    Raises:
        LLMTimeoutError, LLMProviderError, LLMConfigError.
    """
    settings = settings or get_settings()
    backend = settings.llm_provider
    timeout = timeout_seconds or float(settings.llm_request_timeout_seconds)
    max_tok = max_tokens or settings.llm_max_tokens
    temp = temperature if temperature is not None else settings.llm_temperature

    if backend == "tachyon":
        return await _call_tachyon(
            settings=settings,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            timeout=timeout,
            max_tokens=max_tok,
            temperature=temp,
            json_mode=False,
        )
    if backend == "stub":
        return _stub_response(system_prompt, user_prompt, json_mode=False)
    raise LLMConfigError(f"unknown LLM provider: {backend!r}")


async def complete_structured(
    *,
    system_prompt: str,
    user_prompt: str,
    settings: Settings | None = None,
    timeout_seconds: float | None = None,
    max_tokens: int | None = None,
    temperature: float | None = None,
) -> LLMResponse:
    """JSON-mode completion. The model is instructed to emit a single
    JSON object. The caller is responsible for parsing + Pydantic-validation.

    Why we don't parse here:
        Different agents want different shapes; the client shouldn't
        know about MigrationPlanRequest or TopologyRiskBrief. It just
        nudges the backend toward emitting valid JSON and returns the
        raw text. Callers do `json.loads(resp.text)` and validate.

    Tachyon's gateway may not honour an OpenAI-style
    `response_format` knob, so JSON is enforced by prompt instruction
    (see _call_tachyon) and Pydantic-validated by the caller.
    """
    settings = settings or get_settings()
    backend = settings.llm_provider
    timeout = timeout_seconds or float(settings.llm_request_timeout_seconds)
    max_tok = max_tokens or settings.llm_max_tokens
    temp = temperature if temperature is not None else settings.llm_temperature

    if backend == "tachyon":
        return await _call_tachyon(
            settings=settings,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            timeout=timeout,
            max_tokens=max_tok,
            temperature=temp,
            json_mode=True,
        )
    if backend == "stub":
        return _stub_response(system_prompt, user_prompt, json_mode=True)
    raise LLMConfigError(f"unknown LLM provider: {backend!r}")


# ─────────────────────────────────────────────────────────────────────────
# Tachyon backend (WF-internal — via the tachyon-langchain-client package)
# ─────────────────────────────────────────────────────────────────────────


def _get_tachyon_client(settings: Settings) -> Any:
    """Lazy-init the TachyonLangchainClient. Cached module-global.

    Mirrors Phase 1's proven `_get_client()`: load the .env into
    os.environ (the package reads its Apigee creds from there), import
    the package, construct it with only `model_name`. Raises
    LLMConfigError on any failure so the caller falls back deterministically.
    """
    global _tachyon_client, _tachyon_client_failed

    if _tachyon_client is not None:
        return _tachyon_client
    if _tachyon_client_failed:
        raise LLMConfigError(
            "Tachyon client init previously failed this process; "
            "not retrying. Check the package install and .env Apigee creds."
        )

    try:
        # The package reads API_KEY / CONSUMER_KEY / CONSUMER_SECRET /
        # APIGEE_URL / BASE_URL / CERTS_PATH / USE_CASE_ID from os.environ.
        # pydantic-settings populates the Settings object but NOT
        # os.environ, so we must load the .env explicitly here.
        try:
            from dotenv import load_dotenv

            load_dotenv()
        except ImportError:
            # python-dotenv absent — rely on OS env vars already being set.
            logger.warning(
                "python-dotenv not installed; Tachyon will use OS env vars only"
            )

        from tachyon_langchain_client import TachyonLangchainClient

        _tachyon_client = TachyonLangchainClient(
            model_name=settings.tachyon_model
        )
        logger.info(
            "Tachyon client initialized (model=%s)", settings.tachyon_model
        )
        return _tachyon_client
    except ImportError as exc:
        _tachyon_client_failed = True
        raise LLMConfigError(
            "tachyon-langchain-client is not installed in this venv. "
            "Install it or set BCL_LLM_PROVIDER=stub for the offline path."
        ) from exc
    except Exception as exc:  # noqa: BLE001 - any init failure -> fallback
        _tachyon_client_failed = True
        raise LLMConfigError(
            f"failed to initialise Tachyon client: {type(exc).__name__}: {exc}"
        ) from exc


async def _call_tachyon(
    *,
    settings: Settings,
    system_prompt: str,
    user_prompt: str,
    timeout: float,
    max_tokens: int,
    temperature: float,
    json_mode: bool,
) -> LLMResponse:
    """Call Tachyon via the tachyon-langchain-client package.

    The package's client subclasses LangChain's ChatOpenAI, so the call
    shape is `client.invoke(messages)` -> message object with `.content`.
    That call is synchronous and may block (network + Apigee token), so
    it is run in a worker thread to keep the event loop free.

    `max_tokens` / `temperature` are accepted for signature parity with
    the other backends; the Phase-1-proven construction does not pass
    them per call (the client is built once with model_name only). If
    per-call tuning is later needed, that is a change inside this
    function alone.
    """
    client = _get_tachyon_client(settings)  # raises LLMConfigError -> fallback

    # json_mode: Tachyon's gateway may not honour an OpenAI-style
    # response_format knob. We enforce JSON the same way the rest of the
    # system already does — by instruction in the prompt — and the caller
    # (run_structured_agent) validates with Pydantic regardless. Append a
    # terse JSON instruction so a structured call still nudges the model.
    sys_prompt = system_prompt
    if json_mode:
        sys_prompt = (
            system_prompt
            + "\n\nIMPORTANT: Respond with a single valid JSON object only. "
            "No markdown fences, no prose before or after."
        )

    messages = [
        {"role": "system", "content": sys_prompt},
        {"role": "user", "content": user_prompt},
    ]

    t0 = time.monotonic()
    try:
        # Sync .invoke run in a worker thread + an asyncio timeout so a
        # hung gateway cannot stall the migration. wait_for cancels the
        # await; the worker thread is daemonic and abandoned on timeout.
        result = await asyncio.wait_for(
            asyncio.to_thread(client.invoke, messages),
            timeout=timeout,
        )
    except asyncio.TimeoutError as exc:
        raise LLMTimeoutError(f"tachyon timeout after {timeout}s") from exc
    except Exception as exc:  # noqa: BLE001 - any call failure -> fallback
        # Rate-limit / auth / transport all surface here. The caller
        # catches LLMError and runs the deterministic fallback.
        raise LLMProviderError(0, f"tachyon call failed: {exc}") from exc

    duration_ms = int((time.monotonic() - t0) * 1000)

    # ChatOpenAI-style response: a message object with `.content`.
    text = getattr(result, "content", None)
    if not text or not str(text).strip():
        raise LLMProviderError(0, f"tachyon returned empty content: {result!r}")

    # Token usage, if the package surfaces it (LangChain puts it in
    # response_metadata / usage_metadata). Best-effort; None is fine.
    tokens_in: int | None = None
    tokens_out: int | None = None
    usage = getattr(result, "usage_metadata", None)
    if isinstance(usage, dict):
        tokens_in = usage.get("input_tokens")
        tokens_out = usage.get("output_tokens")

    return LLMResponse(
        text=str(text),
        model=f"tachyon:{settings.tachyon_model}",
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        duration_ms=duration_ms,
        backend="tachyon",
        raw_response=None,
    )


# ─────────────────────────────────────────────────────────────────────────
# Stub backend — deterministic, offline
# ─────────────────────────────────────────────────────────────────────────


def _stub_response(
    system_prompt: str, user_prompt: str, *, json_mode: bool
) -> LLMResponse:
    """Generic last-resort backend. Returns a constant shape.

    The Migration Planner has its own domain-aware fallback in
    bcl.agents.planner.deterministic_plan(); this stub is only used
    when LLM_PROVIDER=stub is set explicitly (tests, offline demos)
    and the caller wants the LLM facade to not crash.
    """
    if json_mode:
        text = json.dumps({
            "_stub": True,
            "system_prompt_len": len(system_prompt),
            "user_prompt_len": len(user_prompt),
            "note": (
                "LLM_PROVIDER=stub. No model called. Caller should use "
                "its own deterministic fallback."
            ),
        })
    else:
        text = (
            "[stub backend] No real LLM call was made. "
            f"system={len(system_prompt)} chars, user={len(user_prompt)} chars."
        )
    return LLMResponse(
        text=text,
        model="stub:none",
        tokens_in=len(system_prompt) // 4,
        tokens_out=len(text) // 4,
        duration_ms=1,
        backend="stub",
        raw_response=None,
    )


__all__ = [
    "LLMResponse",
    "LLMError",
    "LLMTimeoutError",
    "LLMProviderError",
    "LLMConfigError",
    "complete_text",
    "complete_structured",
]
