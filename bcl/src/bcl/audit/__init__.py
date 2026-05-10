"""Audit log — Lamport-clocked, append-only, the BCL system of record."""

from bcl.audit.lamport import LamportClock
from bcl.audit.middleware import CorrelationIdMiddleware
from bcl.audit.writer import (
    get_actor,
    get_correlation_id,
    set_actor,
    set_correlation_id,
    write_audit_entry,
)

__all__ = [
    "CorrelationIdMiddleware",
    "LamportClock",
    "get_actor",
    "get_correlation_id",
    "set_actor",
    "set_correlation_id",
    "write_audit_entry",
]
