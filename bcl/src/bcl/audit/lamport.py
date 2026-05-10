"""Lamport logical clock — the BCL's source of causal truth.

Per Lamport (1978), "Time, Clocks, and the Ordering of Events in a Distributed
System": logical clocks order events by causality, not by wall-clock time.
Wall-clock can drift, leap, jump backward; a logical clock cannot.

For the BCL, every audit-log entry receives a Lamport timestamp. When the
rollback engine walks the log in reverse, it walks in REVERSE LAMPORT ORDER —
not reverse wall-clock order.
"""

from __future__ import annotations

import asyncio
from typing import Self

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bcl.models.orm import AuditLog


class LamportClock:
    """Process-singleton monotonic clock."""

    _instance: Self | None = None

    def __init__(self) -> None:
        self._counter: int = 0
        self._lock: asyncio.Lock = asyncio.Lock()
        self._bootstrapped: bool = False

    @classmethod
    def instance(cls) -> LamportClock:
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def reset_for_tests(cls) -> None:
        """Used only in tests. Production code never calls this."""
        cls._instance = None

    async def bootstrap(self, session: AsyncSession) -> None:
        """Load the last-persisted Lamport clock from the audit log."""
        async with self._lock:
            if self._bootstrapped:
                return
            stmt = select(AuditLog.lamport_clock).order_by(
                AuditLog.lamport_clock.desc()
            ).limit(1)
            result = await session.execute(stmt)
            last = result.scalar_one_or_none()
            self._counter = last if last is not None else 0
            self._bootstrapped = True

    async def tick(self) -> int:
        """Increment and return the next Lamport timestamp."""
        async with self._lock:
            self._counter += 1
            return self._counter

    async def peek(self) -> int:
        """Return current value without incrementing."""
        async with self._lock:
            return self._counter
