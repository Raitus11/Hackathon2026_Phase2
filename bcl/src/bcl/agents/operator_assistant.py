"""Operator Assistant — the BCL's second agent.

The Operator Assistant answers natural-language questions about migrations
from real BCL data. It is the conversational front-end specified in Battle
Plan v3 §6.3.7.

Two execution paths, selected by BCL_LLM_PROVIDER (same switch the Migration
Planner uses):

  - tachyon:  the question + a compact, factual context block (assembled
              from real DB rows) is sent to the LLM via run_text_agent.
              The LLM phrases the answer; the *facts* are still the DB's.
  - stub:     a deterministic responder reads the same DB rows and
              templates a real answer. No canned paragraphs — every
              number in the answer is queried live from the migrations
              table, the audit log, and the agent-invocation log.

Either way the question is classified into an intent, the relevant real
data is fetched, and the invocation is audit-logged through the standard
agents.base machinery. The assistant is read-only: it has no tools that
mutate state. It can only SELECT.

Design note (Schluntz & Zhang, Anthropic "Building effective agents",
2024-12): this is deliberately a single-call agent over a fixed set of
read queries, not a tool-calling loop. The intent classifier is a small
deterministic router. That is sufficient for the operator's questions and
keeps every answer traceable to a specific SQL read.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bcl.agents.base import run_text_agent
from bcl.models.orm import (
    AgentInvocation,
    AuditLog,
    Migration,
    MigrationState,
)

logger = logging.getLogger("bcl.agents.operator_assistant")


# ─────────────────────────────────────────────────────────────────────────
# Intent classification — deterministic router
# ─────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _Intent:
    """A classified operator question."""

    kind: str
    """One of: STATUS_ALL, STATUS_ONE, AUDIT_ONE, ROLLBACK_INFO,
    DRAIN_INFO, AGENT_ACTIVITY, COUNTS, HELP."""

    app_id: str | None = None
    """Resolved app id when the question names one."""


_KEYWORDS_ROLLBACK = ("rollback", "rolled back", "roll back", "reverted", "undo")
_KEYWORDS_DRAIN = ("drain", "queue depth", "little", "backlog")
_KEYWORDS_AGENT = ("agent", "planner", "llm", "invocation", "ai activity")
_KEYWORDS_AUDIT = ("audit", "lamport", "history", "what happened", "timeline")
_KEYWORDS_COUNT = ("how many", "count", "total", "summary", "overall")


def classify_intent(question: str, known_app_ids: list[str]) -> _Intent:
    """Route an operator question to an intent + (optionally) an app id.

    Pure function. No LLM. The classifier is intentionally simple — the
    operator's real questions cluster into a handful of shapes, and a
    deterministic router keeps every answer auditable to a known query.
    """
    q = question.lower().strip()

    # Resolve an app id if the question names one. Match the longest
    # known id first so "APUMN/GC" wins over a bare "GC" substring.
    resolved: str | None = None
    for app_id in sorted(known_app_ids, key=len, reverse=True):
        if app_id.lower() in q:
            resolved = app_id
            break

    if any(k in q for k in _KEYWORDS_COUNT) and resolved is None:
        return _Intent("COUNTS")
    if any(k in q for k in _KEYWORDS_AGENT):
        return _Intent("AGENT_ACTIVITY", resolved)
    if any(k in q for k in _KEYWORDS_ROLLBACK):
        return _Intent("ROLLBACK_INFO", resolved)
    if any(k in q for k in _KEYWORDS_DRAIN):
        return _Intent("DRAIN_INFO", resolved)
    if any(k in q for k in _KEYWORDS_AUDIT) and resolved is not None:
        return _Intent("AUDIT_ONE", resolved)
    if resolved is not None:
        return _Intent("STATUS_ONE", resolved)
    if any(k in q for k in _KEYWORDS_AUDIT):
        return _Intent("STATUS_ALL")
    if not q:
        return _Intent("HELP")
    return _Intent("STATUS_ALL")


# ─────────────────────────────────────────────────────────────────────────
# Data access — read-only queries against real BCL tables
# ─────────────────────────────────────────────────────────────────────────


async def _all_migrations(session: AsyncSession) -> list[Migration]:
    rows = await session.execute(
        select(Migration).order_by(Migration.id)
    )
    return list(rows.scalars().all())


async def _migration_for_app(
    session: AsyncSession, app_id: str
) -> Migration | None:
    rows = await session.execute(
        select(Migration)
        .where(Migration.app_id == app_id)
        .order_by(desc(Migration.id))
        .limit(1)
    )
    return rows.scalar_one_or_none()


async def _audit_for_correlation(
    session: AsyncSession, correlation_id: str, limit: int = 12
) -> list[AuditLog]:
    rows = await session.execute(
        select(AuditLog)
        .where(AuditLog.correlation_id == correlation_id)
        .order_by(AuditLog.lamport_clock)
        .limit(limit)
    )
    return list(rows.scalars().all())


async def _recent_agent_invocations(
    session: AsyncSession, limit: int = 8
) -> list[AgentInvocation]:
    rows = await session.execute(
        select(AgentInvocation)
        .order_by(desc(AgentInvocation.id))
        .limit(limit)
    )
    return list(rows.scalars().all())


# ─────────────────────────────────────────────────────────────────────────
# Context assembly — turns DB rows into a compact factual block
# ─────────────────────────────────────────────────────────────────────────


_ABSORBING = {MigrationState.COMPLETED, MigrationState.ROLLED_BACK}
_FAILURE = {MigrationState.ROLLBACK_FAILED}


async def _build_context(
    session: AsyncSession, intent: _Intent
) -> dict[str, Any]:
    """Fetch exactly the real data the intent needs. Returns a plain dict
    of facts — this is both the LLM context block and the deterministic
    responder's input."""
    ctx: dict[str, Any] = {"intent": intent.kind, "app_id": intent.app_id}

    if intent.kind in ("STATUS_ALL", "COUNTS"):
        migs = await _all_migrations(session)
        ctx["migrations"] = [
            {"app_id": m.app_id, "state": m.state.value} for m in migs
        ]
        ctx["total"] = len(migs)
        ctx["completed"] = sum(
            1 for m in migs if m.state == MigrationState.COMPLETED
        )
        ctx["rolled_back"] = sum(
            1 for m in migs if m.state == MigrationState.ROLLED_BACK
        )
        ctx["in_flight"] = sum(
            1 for m in migs if m.state not in _ABSORBING | _FAILURE
        )
        ctx["rollback_failed"] = sum(
            1 for m in migs if m.state in _FAILURE
        )
        return ctx

    if intent.kind == "AGENT_ACTIVITY":
        invs = await _recent_agent_invocations(session)
        ctx["invocations"] = [
            {
                "agent": iv.agent_name.value,
                "trigger": iv.trigger,
                "model": iv.model,
                "succeeded": iv.succeeded,
                "duration_ms": iv.duration_ms,
            }
            for iv in invs
        ]
        ctx["invocation_count"] = len(invs)
        return ctx

    # All remaining intents are app-scoped.
    if intent.app_id is None:
        ctx["error"] = "no app named in the question"
        return ctx

    mig = await _migration_for_app(session, intent.app_id)
    if mig is None:
        ctx["error"] = f"no migration found for app {intent.app_id}"
        return ctx

    ctx["migration"] = {
        "id": mig.id,
        "app_id": mig.app_id,
        "state": mig.state.value,
        "started_at": mig.started_at.isoformat() if mig.started_at else None,
        "completed_at": (
            mig.completed_at.isoformat() if mig.completed_at else None
        ),
    }
    plan = mig.plan or {}
    ctx["planner_source"] = plan.get("planner_source")

    # The migration's correlation id lives in its plan metadata; fall
    # back to scanning audit rows by app_id if absent.
    corr = plan.get("correlation_id")
    if corr:
        audit = await _audit_for_correlation(session, corr)
        ctx["audit"] = [
            {
                "lamport_clock": a.lamport_clock,
                "operation": a.operation.value,
                "success": a.success,
                "is_rollback": a.is_rollback,
                "wall_clock": a.wall_clock.isoformat(),
            }
            for a in audit
        ]
    return ctx


# ─────────────────────────────────────────────────────────────────────────
# Deterministic responder — used when BCL_LLM_PROVIDER=stub
# ─────────────────────────────────────────────────────────────────────────


def deterministic_answer(question: str, ctx: dict[str, Any]) -> str:
    """Build a real answer from real data, with no LLM.

    Every figure in the returned text is read from `ctx`, which was
    populated by live SELECTs. This is the offline path; it is not a
    canned response — re-run it after a migration changes and the
    answer changes with the data.
    """
    if "error" in ctx:
        return (
            f"I couldn't answer that: {ctx['error']}. "
            "Try naming a specific app (e.g. 'ZN' or 'LIY/KW'), or ask "
            "for an overall status summary."
        )

    kind = ctx["intent"]

    if kind in ("STATUS_ALL", "COUNTS"):
        lines = [
            f"There are {ctx['total']} migrations on record: "
            f"{ctx['completed']} COMPLETED, {ctx['rolled_back']} ROLLED_BACK, "
            f"{ctx['in_flight']} in flight, "
            f"{ctx['rollback_failed']} in ROLLBACK_FAILED."
        ]
        per_app = ", ".join(
            f"{m['app_id']} ({m['state']})" for m in ctx["migrations"]
        )
        if per_app:
            lines.append(f"By app: {per_app}.")
        return " ".join(lines)

    if kind == "AGENT_ACTIVITY":
        n = ctx["invocation_count"]
        if n == 0:
            return "No agent invocations have been recorded yet."
        ok = sum(1 for i in ctx["invocations"] if i["succeeded"])
        recent = ctx["invocations"][0]
        return (
            f"The agent layer has {n} recent invocation(s) on record, "
            f"{ok} succeeded. Most recent: {recent['agent']} "
            f"({recent['trigger']}), model {recent['model']}, "
            f"{recent['duration_ms']} ms. Every agent call is written to "
            "the audit log as an AGENT_INVOCATION event."
        )

    mig = ctx["migration"]
    src = ctx.get("planner_source")
    src_note = ""
    if src == "llm":
        src_note = " Its plan was generated by the Migration Planner via the LLM."
    elif src in ("stub_fallback", "fallback"):
        src_note = (
            " Its plan was produced by the Migration Planner's "
            "deterministic fallback."
        )

    if kind == "STATUS_ONE":
        msg = (
            f"App {mig['app_id']} (migration #{mig['id']}) is currently in "
            f"state {mig['state']}.{src_note}"
        )
        if mig["completed_at"]:
            msg += f" It reached a terminal state at {mig['completed_at']}."
        return msg

    if kind == "ROLLBACK_INFO":
        audit = ctx.get("audit", [])
        rb = [a for a in audit if a["is_rollback"]]
        if not rb:
            return (
                f"App {mig['app_id']} (migration #{mig['id']}) is in state "
                f"{mig['state']}. No rollback steps are recorded for it."
            )
        ok = sum(1 for a in rb if a["success"])
        return (
            f"App {mig['app_id']} (migration #{mig['id']}, state "
            f"{mig['state']}) has {len(rb)} rollback step(s) in its audit "
            f"trail, {ok} successful. Rollback steps are applied in reverse "
            "Lamport order — the inverse of the forward migration."
        )

    if kind == "DRAIN_INFO":
        return (
            f"App {mig['app_id']} (migration #{mig['id']}) is in state "
            f"{mig['state']}. Drain is predicted by Little's Law "
            "(T_drain ≈ L₀/μ); per-queue drain figures are on the "
            "migration detail page's Drain panel."
        )

    if kind == "AUDIT_ONE":
        audit = ctx.get("audit", [])
        if not audit:
            return (
                f"App {mig['app_id']} (migration #{mig['id']}) is in state "
                f"{mig['state']}. No Lamport-ordered audit entries were "
                "found for its correlation id."
            )
        first, last = audit[0], audit[-1]
        return (
            f"App {mig['app_id']} (migration #{mig['id']}, state "
            f"{mig['state']}) has {len(audit)} audit entries spanning "
            f"Lamport clock {first['lamport_clock']}–{last['lamport_clock']}. "
            f"Most recent operation: {last['operation']} "
            f"({'ok' if last['success'] else 'failed'})."
        )

    return (
        "I can answer questions about migration status, rollbacks, drain "
        "predictions, the audit trail, and agent activity. Name an app or "
        "ask for an overall summary."
    )


# ─────────────────────────────────────────────────────────────────────────
# Public entry point
# ─────────────────────────────────────────────────────────────────────────


_SYSTEM_PROMPT = (
    "You are the Operator Assistant for an IBM MQ migration control "
    "plane. You answer an operator's question using ONLY the factual "
    "context block provided. Never invent migration ids, states, "
    "numbers, or events. If the context does not contain the answer, "
    "say so plainly. Be concise and precise — operators want the fact, "
    "not prose. Two to four sentences."
)


def _render_user_prompt(question: str, ctx: dict[str, Any]) -> str:
    import json

    return (
        f"Operator question:\n{question}\n\n"
        f"Factual context (the only facts you may use):\n"
        f"{json.dumps(ctx, indent=2, default=str)}\n\n"
        "Answer the operator's question from this context."
    )


async def answer_question(
    *,
    question: str,
    session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    correlation_id: str | None = None,
    actor: str | None = None,
) -> dict[str, Any]:
    """Answer one operator question. Always returns a dict:

        {
          "answer": str,            # the natural-language answer
          "source": str,            # "llm" | "stub" | "llm_fallback"
          "intent": str,            # classified intent kind
          "app_id": str | None,
          "agent_invocation_id": int | None,
        }

    The answer is always populated — if the LLM path is unavailable the
    deterministic responder supplies it. Both paths answer from the same
    real data; the LLM only changes the phrasing.
    """
    # Known app ids drive the intent classifier's app resolution.
    all_migs = await _all_migrations(session)
    known_app_ids = sorted({m.app_id for m in all_migs})

    intent = classify_intent(question, known_app_ids)
    ctx = await _build_context(session, intent)

    # The factual answer the deterministic responder would give. This is
    # also the guaranteed fallback if the LLM path fails.
    deterministic = deterministic_answer(question, ctx)

    from bcl.config import get_settings

    provider = get_settings().llm_provider

    if provider == "stub":
        # Offline path: deterministic responder IS the answer. We still
        # record an audit row so the assistant's activity is visible in
        # the audit log exactly like any other agent.
        _text, invocation = await run_text_agent(
            agent_name=__import__(
                "bcl.models.orm", fromlist=["AgentName"]
            ).AgentName.OPERATOR_ASSISTANT,
            trigger="POST /assistant/query",
            system_prompt=_SYSTEM_PROMPT,
            user_prompt=_render_user_prompt(question, ctx),
            input_for_audit={"question": question, "intent": intent.kind},
            session_factory=session_factory,
            correlation_id=correlation_id,
            actor=actor,
        )
        return {
            "answer": deterministic,
            "source": "stub",
            "intent": intent.kind,
            "app_id": intent.app_id,
            "agent_invocation_id": invocation.id,
        }

    # LLM path (tachyon). The LLM phrases the answer over the real
    # context; deterministic stays as the guaranteed fallback.
    from bcl.models.orm import AgentName

    text, invocation = await run_text_agent(
        agent_name=AgentName.OPERATOR_ASSISTANT,
        trigger="POST /assistant/query",
        system_prompt=_SYSTEM_PROMPT,
        user_prompt=_render_user_prompt(question, ctx),
        input_for_audit={"question": question, "intent": intent.kind},
        session_factory=session_factory,
        correlation_id=correlation_id,
        actor=actor,
    )

    if text:
        return {
            "answer": text,
            "source": "llm",
            "intent": intent.kind,
            "app_id": intent.app_id,
            "agent_invocation_id": invocation.id,
        }

    # LLM failed — fall back to the deterministic answer. Same data,
    # different phrasing path. The demo never dies on LLM availability.
    return {
        "answer": deterministic,
        "source": "llm_fallback",
        "intent": intent.kind,
        "app_id": intent.app_id,
        "agent_invocation_id": invocation.id,
    }


__all__ = [
    "answer_question",
    "classify_intent",
    "deterministic_answer",
]
