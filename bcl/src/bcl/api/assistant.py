"""Operator Assistant REST endpoint.

POST /assistant/query   ask the Operator Assistant a question

The Operator Assistant is the BCL's second agent (Battle Plan v3 §6.3.7).
It answers natural-language questions about migrations from real BCL data.

The endpoint is read-only — the assistant can only SELECT. It cannot
start, roll back, or modify a migration. Every query is audit-logged as
an AGENT_INVOCATION through the standard agents.base machinery, so the
assistant's activity is visible in the audit log exactly like the
Migration Planner's.

Response shape is deliberately a single JSON object, not an SSE stream:
the answer is short, and a plain response removes a streaming-failure
mode from the demo. If token streaming is wanted later it is an additive
change behind the same route.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from bcl.agents.operator_assistant import answer_question
from bcl.db.session import get_session, get_session_factory
from bcl.models.api import ChatRequest

router = APIRouter(prefix="/assistant", tags=["assistant"])


class AssistantAnswer(BaseModel):
    """POST /assistant/query response."""

    answer: str
    """The natural-language answer. Always populated."""

    source: str
    """How the answer was produced: 'llm' (Tachyon phrased it),
    'stub' (deterministic responder, offline path), or 'llm_fallback'
    (LLM was tried and failed; deterministic answer returned)."""

    intent: str
    """The classified intent of the question."""

    app_id: str | None
    """The app the question was about, if one was named."""

    agent_invocation_id: int | None
    """The AgentInvocation row id — links this answer to the audit log."""


@router.post(
    "/query",
    response_model=AssistantAnswer,
    summary="Ask the Operator Assistant a question",
    description=(
        "The Operator Assistant answers questions about migrations from "
        "real BCL data — migration state, the Lamport-ordered audit "
        "trail, rollback steps, drain predictions, and agent activity.\n\n"
        "Read-only: the assistant can only query. It cannot start or "
        "roll back a migration.\n\n"
        "When BCL_LLM_PROVIDER=tachyon the answer is phrased by the LLM "
        "over a factual context block; when =stub a deterministic "
        "responder builds the answer from the same data. Either way the "
        "facts come from live database reads, and the invocation is "
        "audit-logged as an AGENT_INVOCATION event."
    ),
)
async def query_assistant(
    body: ChatRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> AssistantAnswer:
    result: dict[str, Any] = await answer_question(
        question=body.message,
        session=session,
        session_factory=get_session_factory(),
        actor="operator:chat",
    )
    return AssistantAnswer(
        answer=result["answer"],
        source=result["source"],
        intent=result["intent"],
        app_id=result["app_id"],
        agent_invocation_id=result["agent_invocation_id"],
    )
