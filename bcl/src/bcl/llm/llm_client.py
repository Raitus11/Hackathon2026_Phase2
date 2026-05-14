"""LLM client — provider-agnostic facade over Groq / Tachyon / stub.

The BCL talks to language models through exactly this module. Agents
import `complete_structured` (or `complete_text` for free-form output)
and never reach for HTTP directly.

Backends:

  - groq:    public Groq inference API. Used on home laptops where
             Tachyon is unreachable. Free tier; rate-limited.
  - tachyon: Wells Fargo's internal LLM gateway exposing Gemini 2.5
             Pro and friends. Used on office laptops where the network
             reaches https://tachyon.internal.wellsfargo.com (or similar
             - the actual URL is in BCL_TACHYON_ENDPOINT).
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

  - Groq API: https://console.groq.com/docs/api-reference
  - Gemini structured output: https://ai.google.dev/gemini-api/docs/structured-output
  - Anthropic, "Building effective agents" (2024-12).
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Literal

import httpx

from bcl.config import Settings, get_settings

logger = logging.getLogger("bcl.llm.client")


# ─────────────────────────────────────────────────────────────────────────
# Response shape
# ─────────────────────────────────────────────────────────────────────────


@dataclass
class LLMResponse:
    """One LLM completion result.

    `model` is the fully-qualified model identifier including the
    backend prefix (e.g. 'groq:llama-3.3-70b-versatile'). This goes
    straight into AgentInvocation.model so the audit log can answer
    "which model said what" without separate joins.
    """

    text: str
    """The model's textual output. For structured-output requests
    the caller must json.loads this; the client does not parse."""

    model: str
    """Backend-prefixed model identifier — e.g. 'groq:llama-3.3-70b-versatile'."""

    tokens_in: int | None
    tokens_out: int | None
    duration_ms: int

    backend: Literal["groq", "tachyon", "stub"]
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

    if backend == "groq":
        return await _call_groq(
            settings=settings,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            timeout=timeout,
            max_tokens=max_tok,
            temperature=temp,
            json_mode=False,
        )
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

    Both Groq and Tachyon expose a `response_format={type: json_object}`
    knob (OpenAI-compatible). We pass it where supported.
    """
    settings = settings or get_settings()
    backend = settings.llm_provider
    timeout = timeout_seconds or float(settings.llm_request_timeout_seconds)
    max_tok = max_tokens or settings.llm_max_tokens
    temp = temperature if temperature is not None else settings.llm_temperature

    if backend == "groq":
        return await _call_groq(
            settings=settings,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            timeout=timeout,
            max_tokens=max_tok,
            temperature=temp,
            json_mode=True,
        )
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
# Groq backend (OpenAI-compatible)
# ─────────────────────────────────────────────────────────────────────────


_GROQ_ENDPOINT = "https://api.groq.com/openai/v1/chat/completions"


async def _call_groq(
    *,
    settings: Settings,
    system_prompt: str,
    user_prompt: str,
    timeout: float,
    max_tokens: int,
    temperature: float,
    json_mode: bool,
) -> LLMResponse:
    if not settings.groq_api_key:
        raise LLMConfigError(
            "BCL_LLM_PROVIDER=groq but BCL_GROQ_API_KEY is empty"
        )

    body: dict[str, Any] = {
        "model": settings.groq_model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    if json_mode:
        body["response_format"] = {"type": "json_object"}

    headers = {
        "Authorization": f"Bearer {settings.groq_api_key}",
        "Content-Type": "application/json",
    }

    t0 = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(_GROQ_ENDPOINT, json=body, headers=headers)
    except httpx.TimeoutException as exc:
        raise LLMTimeoutError(f"groq timeout after {timeout}s") from exc
    except httpx.HTTPError as exc:
        raise LLMProviderError(0, f"transport error: {exc}") from exc

    duration_ms = int((time.monotonic() - t0) * 1000)

    if resp.status_code >= 400:
        raise LLMProviderError(resp.status_code, resp.text)

    data = resp.json()
    try:
        text = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError) as exc:
        raise LLMProviderError(
            resp.status_code, f"unexpected response shape: {data}"
        ) from exc

    usage = data.get("usage", {}) or {}
    return LLMResponse(
        text=text or "",
        model=f"groq:{settings.groq_model}",
        tokens_in=usage.get("prompt_tokens"),
        tokens_out=usage.get("completion_tokens"),
        duration_ms=duration_ms,
        backend="groq",
        raw_response=data,
    )


# ─────────────────────────────────────────────────────────────────────────
# Tachyon backend (WF-internal — assumed OpenAI-compatible per common pattern)
# ─────────────────────────────────────────────────────────────────────────


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
    if not settings.tachyon_endpoint:
        raise LLMConfigError(
            "BCL_LLM_PROVIDER=tachyon but BCL_TACHYON_ENDPOINT is empty"
        )

    # Tachyon is assumed to expose an OpenAI-compatible /v1/chat/completions.
    # If the real endpoint differs, this is the single place to adapt.
    url = settings.tachyon_endpoint.rstrip("/") + "/v1/chat/completions"
    body: dict[str, Any] = {
        "model": settings.tachyon_model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    if json_mode:
        body["response_format"] = {"type": "json_object"}

    headers: dict[str, str] = {"Content-Type": "application/json"}
    if settings.tachyon_api_key:
        headers["Authorization"] = f"Bearer {settings.tachyon_api_key}"

    t0 = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=timeout, verify=False) as client:
            # verify=False: WF internal endpoints often have self-signed
            # certs. In production this would be replaced with the WF
            # corporate CA bundle.
            resp = await client.post(url, json=body, headers=headers)
    except httpx.TimeoutException as exc:
        raise LLMTimeoutError(f"tachyon timeout after {timeout}s") from exc
    except httpx.HTTPError as exc:
        raise LLMProviderError(0, f"transport error: {exc}") from exc

    duration_ms = int((time.monotonic() - t0) * 1000)

    if resp.status_code >= 400:
        raise LLMProviderError(resp.status_code, resp.text)

    data = resp.json()
    try:
        text = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError) as exc:
        raise LLMProviderError(
            resp.status_code, f"unexpected response shape: {data}"
        ) from exc

    usage = data.get("usage", {}) or {}
    return LLMResponse(
        text=text or "",
        model=f"tachyon:{settings.tachyon_model}",
        tokens_in=usage.get("prompt_tokens"),
        tokens_out=usage.get("completion_tokens"),
        duration_ms=duration_ms,
        backend="tachyon",
        raw_response=data,
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
    when LLM_PROVIDER=stub is set explicitly (tests, offline demos
    with no Groq key, etc.) and the caller wants the LLM facade to
    not crash.
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
