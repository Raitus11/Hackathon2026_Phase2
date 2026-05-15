"""Agent runner — the thin layer that wraps every LLM-touched code path
with the same audit-grade record-keeping.

Every agent in the BCL produces exactly one `AgentInvocation` ORM row
per invocation. That row captures the input, the output, the tools
called, the model, the duration, and whether the call succeeded.
Without this row the LLM is invisible to the audit log — and the
"system of record" claim falls apart.

This module is intentionally short. Per Schluntz & Zhang (Anthropic
"Building effective agents", 2024-12): start with the simplest
possible agent. A single LLM call wrapped in a Pydantic validator
with a deterministic fallback is an agent. It does not need a graph.
It does not need a supervisor. When we have three of them, we'll
revisit.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from datetime import UTC, datetime
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bcl.audit.writer import get_correlation_id, write_audit_entry
from bcl.llm.llm_client import (
    LLMError,
    LLMResponse,
    complete_structured,
    complete_text,
)
from bcl.models.orm import AgentInvocation, AgentName, AuditOperation

logger = logging.getLogger("bcl.agents.base")


T = TypeVar("T", bound=BaseModel)


# ─────────────────────────────────────────────────────────────────────────
# Structured (JSON-output) agent runner
# ─────────────────────────────────────────────────────────────────────────


async def run_structured_agent(
    *,
    agent_name: AgentName,
    trigger: str,
    system_prompt: str,
    user_prompt: str,
    input_for_audit: dict[str, Any],
    output_schema: type[T],
    session_factory: async_sessionmaker[AsyncSession],
    correlation_id: str | None = None,
    actor: str | None = None,
) -> tuple[T | None, AgentInvocation]:
    """Run an LLM-backed structured-output agent.

    Returns `(parsed_output, audit_row)`. `parsed_output` is None if the
    LLM call failed OR the response could not be parsed as `output_schema`;
    the caller is responsible for the fallback path. The audit row is
    persisted either way.

    Why None instead of raising:
        Agents are best-effort. The caller (e.g. the migration engine)
        always has a deterministic fallback. Raising would couple
        flow-control to LLM availability, which is the opposite of
        what we want.

    Why the caller passes `input_for_audit` separately from the prompts:
        The system prompt and user prompt may contain expensive-to-store
        boilerplate (schema descriptions, examples). The audit row
        captures the *semantic* input — the structured dict that drove
        the prompt — which is what an auditor would actually want to
        replay.
    """
    cid = correlation_id or get_correlation_id()
    actor = actor or f"agent:{agent_name.value}"

    t0 = time.monotonic()
    started_at = datetime.now(UTC)

    parsed: T | None = None
    llm_resp: LLMResponse | None = None
    succeeded = False
    error: str | None = None
    output_text: str | None = None
    output_dict: dict[str, Any] | None = None
    model_id = f"{_provider_label()}:unknown"

    # Stub provider short-circuit. When BCL_LLM_PROVIDER=stub the LLM
    # backend deliberately returns a placeholder JSON ({"_stub": True})
    # that cannot validate against any real output schema. Going through
    # the normal validation path produces a noisy red ValidationError
    # in the audit UI for what is actually expected behaviour — the
    # caller's deterministic fallback is the intended code path.
    #
    # Short-circuit: skip the call, write a clean success audit row
    # noting the stub backend, return parsed=None so the caller's
    # fallback runs as designed.
    if _provider_label() == "stub":
        duration_ms = int((time.monotonic() - t0) * 1000)
        audit_row = await _persist_invocation(
            session_factory=session_factory,
            agent_name=agent_name,
            trigger=trigger,
            correlation_id=cid,
            actor=actor,
            input_for_audit=input_for_audit,
            output_text=None,
            output_dict={
                "_stub_short_circuit": True,
                "note": (
                    "BCL_LLM_PROVIDER=stub. Skipped LLM call. "
                    "Deterministic fallback used by caller."
                ),
            },
            model_id="stub:none",
            tokens_in=0,
            tokens_out=0,
            duration_ms=duration_ms,
            succeeded=True,
            error=None,
            started_at=started_at,
        )
        return None, audit_row

    try:
        llm_resp = await complete_structured(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
        )
        output_text = llm_resp.text
        model_id = llm_resp.model
        try:
            output_dict = json.loads(llm_resp.text)
            parsed = output_schema.model_validate(output_dict)
            succeeded = True
        except (json.JSONDecodeError, ValidationError) as exc:
            error = f"agent output failed validation: {exc}"
            logger.warning(
                "agent %s output validation failed: %s",
                agent_name.value, exc,
            )
    except LLMError as exc:
        error = f"LLM call failed: {type(exc).__name__}: {exc}"
        logger.warning("agent %s LLM call failed: %s", agent_name.value, exc)

    duration_ms = int((time.monotonic() - t0) * 1000)

    audit_row = await _persist_invocation(
        session_factory=session_factory,
        agent_name=agent_name,
        trigger=trigger,
        correlation_id=cid,
        actor=actor,
        input_for_audit=input_for_audit,
        output_text=output_text,
        output_dict=output_dict,
        model_id=model_id,
        tokens_in=llm_resp.tokens_in if llm_resp else None,
        tokens_out=llm_resp.tokens_out if llm_resp else None,
        duration_ms=duration_ms,
        succeeded=succeeded,
        error=error,
        started_at=started_at,
    )

    return parsed, audit_row


# ─────────────────────────────────────────────────────────────────────────
# Free-text agent runner — for chat / narrator
# ─────────────────────────────────────────────────────────────────────────


async def run_text_agent(
    *,
    agent_name: AgentName,
    trigger: str,
    system_prompt: str,
    user_prompt: str,
    input_for_audit: dict[str, Any],
    session_factory: async_sessionmaker[AsyncSession],
    correlation_id: str | None = None,
    actor: str | None = None,
) -> tuple[str | None, AgentInvocation]:
    """Run an LLM-backed free-text agent. Returns (text, audit_row).

    Used by the Operator Assistant (chat answers) and the Compliance
    Narrator (markdown narrative). Same audit-grade record-keeping as
    `run_structured_agent`.
    """
    cid = correlation_id or get_correlation_id()
    actor = actor or f"agent:{agent_name.value}"

    t0 = time.monotonic()
    started_at = datetime.now(UTC)

    text: str | None = None
    llm_resp: LLMResponse | None = None
    succeeded = False
    error: str | None = None
    model_id = f"{_provider_label()}:unknown"

    # Stub provider short-circuit (see run_structured_agent for full
    # rationale). The text-agent stub would otherwise return a
    # placeholder string that the caller would treat as a real LLM
    # narrative. Skip cleanly and let the caller's deterministic
    # fallback produce the actual deliverable.
    if _provider_label() == "stub":
        duration_ms = int((time.monotonic() - t0) * 1000)
        audit_row = await _persist_invocation(
            session_factory=session_factory,
            agent_name=agent_name,
            trigger=trigger,
            correlation_id=cid,
            actor=actor,
            input_for_audit=input_for_audit,
            output_text=None,
            output_dict={
                "_stub_short_circuit": True,
                "note": (
                    "BCL_LLM_PROVIDER=stub. Skipped LLM call. "
                    "Deterministic fallback used by caller."
                ),
            },
            model_id="stub:none",
            tokens_in=0,
            tokens_out=0,
            duration_ms=duration_ms,
            succeeded=True,
            error=None,
            started_at=started_at,
        )
        return None, audit_row

    try:
        llm_resp = await complete_text(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
        )
        text = llm_resp.text
        model_id = llm_resp.model
        succeeded = bool(text)
    except LLMError as exc:
        error = f"LLM call failed: {type(exc).__name__}: {exc}"
        logger.warning("agent %s LLM call failed: %s", agent_name.value, exc)

    duration_ms = int((time.monotonic() - t0) * 1000)

    audit_row = await _persist_invocation(
        session_factory=session_factory,
        agent_name=agent_name,
        trigger=trigger,
        correlation_id=cid,
        actor=actor,
        input_for_audit=input_for_audit,
        output_text=text,
        output_dict=None,
        model_id=model_id,
        tokens_in=llm_resp.tokens_in if llm_resp else None,
        tokens_out=llm_resp.tokens_out if llm_resp else None,
        duration_ms=duration_ms,
        succeeded=succeeded,
        error=error,
        started_at=started_at,
    )

    return text, audit_row


# ─────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────


def _provider_label() -> str:
    """Return the active provider as a string. Used as the model prefix
    on the rare path where the call never produced an LLMResponse."""
    from bcl.config import get_settings
    return get_settings().llm_provider


async def _persist_invocation(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    agent_name: AgentName,
    trigger: str,
    correlation_id: str,
    actor: str,
    input_for_audit: dict[str, Any],
    output_text: str | None,
    output_dict: dict[str, Any] | None,
    model_id: str,
    tokens_in: int | None,
    tokens_out: int | None,
    duration_ms: int,
    succeeded: bool,
    error: str | None,
    started_at: datetime,
) -> AgentInvocation:
    """Write one AgentInvocation + one AuditLog row in one transaction."""
    summary = json.dumps(input_for_audit, default=str)[:1000]

    async with session_factory() as session:
        invocation = AgentInvocation(
            correlation_id=correlation_id,
            agent_name=agent_name,
            trigger=trigger,
            input_summary=summary,
            input_full=input_for_audit,
            output=output_dict,
            output_text=output_text,
            tools_called=[],  # v1 agents do not call tools
            model=model_id,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            duration_ms=duration_ms,
            succeeded=succeeded,
            error_message=error,
            started_at=started_at,
        )
        session.add(invocation)
        await session.flush()

        # Twin audit-log entry — agent invocations are first-class
        # events. Linking by correlation_id + AGENT_INVOCATION op makes
        # them filterable in the audit UI.
        await write_audit_entry(
            session,
            operation=AuditOperation.AGENT_INVOCATION,
            success=succeeded,
            actor=actor,
            correlation_id=correlation_id,
            request_payload={
                "agent_name": agent_name.value,
                "trigger": trigger,
                "input": input_for_audit,
                "model": model_id,
            },
            response_payload={
                "succeeded": succeeded,
                "output_present": bool(output_text or output_dict),
                "tokens_in": tokens_in,
                "tokens_out": tokens_out,
            },
            error_message=error,
            duration_ms=duration_ms,
        )

        await session.commit()
        return invocation


__all__ = [
    "run_structured_agent",
    "run_text_agent",
]
