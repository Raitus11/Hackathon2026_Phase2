"""Health endpoints — liveness, readiness."""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from bcl import __version__
from bcl.audit.lamport import LamportClock
from bcl.config import get_settings
from bcl.db.session import get_session
from bcl.models.api import HealthOut

router = APIRouter(tags=["health"])


@router.get("/health/live", status_code=status.HTTP_200_OK,
            summary="Liveness probe")
async def liveness() -> dict[str, Literal["ok"]]:
    return {"status": "ok"}


@router.get("/health/ready", status_code=status.HTTP_200_OK,
            response_model=HealthOut, summary="Readiness probe")
async def readiness(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> HealthOut:
    settings = get_settings()
    clock = LamportClock.instance()

    db_ok = False
    try:
        await session.execute(text("SELECT 1"))
        db_ok = True
    except Exception:
        db_ok = False

    if not clock._bootstrapped and db_ok:
        await clock.bootstrap(session)

    k8s_ok = False
    mq_total = 0
    mq_reachable = 0

    if db_ok and (settings.environment == "dev" or k8s_ok):
        overall: Literal["healthy", "degraded", "unhealthy"] = "healthy"
    elif db_ok:
        overall = "degraded"
    else:
        overall = "unhealthy"

    return HealthOut(
        status=overall,
        bcl_version=__version__,
        db_reachable=db_ok,
        k8s_reachable=k8s_ok,
        mq_reachable_count=mq_reachable,
        mq_total_count=mq_total,
        lamport_clock=await clock.peek(),
    )
