"""Correlation-ID middleware."""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

from bcl.audit.writer import set_actor, set_correlation_id


class CorrelationIdMiddleware(BaseHTTPMiddleware):
    """Per-request correlation ID + actor binding."""

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        cid = request.headers.get("X-Correlation-Id") or str(uuid.uuid4())
        set_correlation_id(cid)

        actor_header = request.headers.get("X-Actor")
        actor = f"operator:{actor_header}" if actor_header else "operator:anon"
        set_actor(actor)

        response = await call_next(request)
        response.headers["X-Correlation-Id"] = cid
        return response
