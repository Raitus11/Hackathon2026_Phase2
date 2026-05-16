"""BCL configuration. Driven by environment variables, with sane defaults.

Every config knob the BCL reads must be visible here. No constants
buried in modules. If it changes between dev/staging/prod, it's a Setting.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="BCL_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ─── App ──────────────────────────────────────────────────────────
    app_name: str = "intelliai-2-bcl"
    app_version: str = "0.1.0"
    team_name: str = "intelliAI2DotO"
    product_name: str = "IntelliAI 2.0"
    environment: Literal["dev", "staging", "prod"] = "dev"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    # ─── Database ─────────────────────────────────────────────────────
    # SQLite + aiosqlite. Path is the PVC mount (/data) in production,
    # repo-local for dev.
    db_path: Path = Field(default=Path("./local-data/bcl.db"))

    @property
    def database_url(self) -> str:
        # `sqlite+aiosqlite:////absolute/path` — note 4 slashes for absolute
        return f"sqlite+aiosqlite:///{self.db_path.resolve()}"

    # SQLite tuning. WAL gets us concurrent reads + crash safety.
    sqlite_journal_mode: str = "WAL"
    sqlite_synchronous: str = "NORMAL"   # WAL+NORMAL is the recommended pair
    sqlite_busy_timeout_ms: int = 5000
    sqlite_foreign_keys: bool = True

    # ─── Kubernetes / OpenShift ──────────────────────────────────────
    namespace: str = "roco-dev"
    in_cluster: bool = False
    """When true, BCL uses the in-cluster ServiceAccount; when false, ~/.kube/config."""

    kube_request_timeout_seconds: int = 30

    # ─── MQ image ────────────────────────────────────────────────────
    mq_image: str = (
        "wfcr-proxy-a.wellsfargo.net/docker-icr-mq4u-rremote/ibm-messaging/mq:9.4.5.0-r2"
    )
    mq_admin_password: str = "admin"
    mq_app_password: str = "password"
    mq_listener_port: int = 1414
    mq_web_port: int = 9443
    mq_pod_cpu_request: str = "500m"
    mq_pod_cpu_limit: str = "1"
    mq_pod_memory_request: str = "1Gi"
    mq_pod_memory_limit: str = "2Gi"

    # MQSC delivery — kubectl exec primary, REST for read paths
    mqsc_exec_timeout_seconds: int = 30
    mqsc_admin_rest_timeout_seconds: int = 10

    # ─── LLM provider ────────────────────────────────────────────────
    # Office laptop: tachyon. stub: offline deterministic path.
    llm_provider: Literal["tachyon", "stub"] = "stub"

    # Tachyon (office laptop)
    tachyon_endpoint: str = ""
    tachyon_model: str = "gemini-2.5-pro"
    tachyon_api_key: str = ""

    llm_request_timeout_seconds: int = 60
    llm_max_tokens: int = 2048
    llm_temperature: float = 0.2
    """Low temp for the planner — repeatable plans matter more than creativity."""

    # ─── Agents ──────────────────────────────────────────────────────
    agent_max_tool_calls_per_invocation: int = 8
    """Per-invocation budget cap. Prevents runaway loops."""

    agent_per_minute_rate_limit: int = 30

    # ─── Migration ───────────────────────────────────────────────────
    drain_wait_timeout_seconds: int = 300
    """Hard requirement: drain timeout with explicit fallback."""

    drain_poll_interval_ms: int = 500
    """Sub-second polling for drain status."""

    drain_zero_window_polls: int = 3
    """Number of consecutive zero-depth + zero-IPPROCS/OPPROCS polls to confirm
    drain. Prevents off-by-one false-positive on transient zero readings."""

    # ─── Evidence ────────────────────────────────────────────────────
    evidence_root_path: Path = Field(default=Path("./local-data/evidence"))

    # ─── HTTP ────────────────────────────────────────────────────────
    cors_allow_origins: list[str] = Field(
        default_factory=lambda: ["http://localhost:3000"]
    )

    # ─── Audit ───────────────────────────────────────────────────────
    audit_max_payload_bytes: int = 256 * 1024
    """Per-payload cap. Prevents an oversized MQSC response from blowing the row."""


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached settings instance. Cache invalidates on process restart only."""
    return Settings()
