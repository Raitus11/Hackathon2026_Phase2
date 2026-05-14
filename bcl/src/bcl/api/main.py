"""FastAPI app — the BCL HTTP entry point."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from bcl import __version__
from bcl.api import audit, health, message_flow, migration, provisioning, topology
from bcl.audit.lamport import LamportClock
from bcl.audit.middleware import CorrelationIdMiddleware
from bcl.config import get_settings
from bcl.db.session import dispose_engine, get_engine, get_session_factory
from bcl.models.orm import Base

logger = logging.getLogger("bcl.api.main")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    logger.info(
        "starting %s v%s in %s mode (namespace=%s)",
        settings.app_name, __version__, settings.environment, settings.namespace,
    )
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("database schema verified (%d tables)", len(Base.metadata.tables))
    session_factory = get_session_factory()
    async with session_factory() as session:
        await LamportClock.instance().bootstrap(session)
    logger.info("Lamport clock bootstrapped to %d", await LamportClock.instance().peek())
    yield
    logger.info("shutting down %s", settings.app_name)
    await dispose_engine()


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="IntelliAI 2.0 — Business Control Layer",
        description=(
            "REST API for the IntelliAI 2.0 IBM MQ migration control plane.\n\n"
            "**Team:** intelliAI2DotO  **Event:** Wells Fargo Hackathon 2026 — Phase 2"
        ),
        version=__version__,
        contact={"name": "intelliAI2DotO"},
        openapi_url="/openapi.json",
        docs_url="/docs",
        redoc_url="/redoc",
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_allow_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["X-Correlation-Id"],
    )
    app.add_middleware(CorrelationIdMiddleware)
    app.include_router(health.router)
    app.include_router(topology.router)
    app.include_router(provisioning.router)
    app.include_router(message_flow.router)
    app.include_router(migration.router)
    app.include_router(audit.router)

    @app.get("/", include_in_schema=False)
    async def root() -> dict[str, str]:
        return {
            "service": settings.app_name,
            "product": settings.product_name,
            "team": settings.team_name,
            "version": __version__,
            "docs": "/docs",
            "openapi": "/openapi.json",
        }

    return app


app = create_app()
