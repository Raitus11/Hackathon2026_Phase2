"""SQLAlchemy ORM models + Pydantic API schemas.

ORM models (DB shape) and API schemas (wire shape) are intentionally
SEPARATE. We translate at the controller boundary.
"""

from bcl.models import api, orm
from bcl.models.orm import (
    AgentInvocation,
    AgentName,
    Application,
    AuditLog,
    AuditOperation,
    Base,
    ChannelStatus,
    ChannelType,
    EvidenceBundle,
    KnowledgeEntry,
    Migration,
    MigrationState,
    MigrationStep,
    QueueManager,
    QueueType,
    Topology,
    TopologyKind,
    ValidationKind,
    ValidationOutcome,
    ValidationRun,
)

__all__ = [
    "AgentInvocation",
    "AgentName",
    "Application",
    "AuditLog",
    "AuditOperation",
    "Base",
    "ChannelStatus",
    "ChannelType",
    "EvidenceBundle",
    "KnowledgeEntry",
    "Migration",
    "MigrationState",
    "MigrationStep",
    "QueueManager",
    "QueueType",
    "Topology",
    "TopologyKind",
    "ValidationKind",
    "ValidationOutcome",
    "ValidationRun",
    "api",
    "orm",
]
