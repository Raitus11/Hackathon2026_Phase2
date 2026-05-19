"""HTTP API — FastAPI routers and the app factory.

This package's ``__init__`` is intentionally side-effect free. It does
NOT eagerly import ``bcl.api.main`` — doing so created a circular
import: ``main`` imports the router submodules via ``from bcl.api
import ...``, which runs this ``__init__``, which would re-enter
``main`` before it finished initialising.

``app`` and ``create_app`` remain importable from this package via a
lazy ``__getattr__`` (PEP 562), so ``from bcl.api import app`` keeps
working — the import just happens on first access, after the submodule
graph has finished loading.
"""

from __future__ import annotations

from typing import Any

__all__ = ["app", "create_app"]


def __getattr__(name: str) -> Any:
    """Lazily resolve ``app`` / ``create_app`` from ``bcl.api.main``."""
    if name in ("app", "create_app"):
        from bcl.api.main import app, create_app

        return {"app": app, "create_app": create_app}[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
