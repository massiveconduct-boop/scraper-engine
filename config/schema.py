# config/schema.py
from __future__ import annotations

from pydantic import BaseModel, Field


class LevelConfig(BaseModel):
    engine: str
    proxy_tier_min_score: float
    timeout_seconds: int
    capsolver_enabled: bool = False


class LevelsConfig(BaseModel):
    level_1: LevelConfig = LevelConfig(
        engine="scrapling", proxy_tier_min_score=40.0, timeout_seconds=20
    )
    level_2: LevelConfig = LevelConfig(
        engine="botasaurus+camoufox",
        proxy_tier_min_score=70.0,
        timeout_seconds=40,
        capsolver_enabled=True,
    )
    level_3: LevelConfig = LevelConfig(
        engine="camoufox",
        proxy_tier_min_score=90.0,
        timeout_seconds=60,
        capsolver_enabled=True,
    )


class CamoufoxConfig(BaseModel):
    geoip: bool = True
    humanize: float = Field(default=1.5, ge=0.0, le=5.0)
    headless_mode: str = "virtual"
    max_total_instances: int = 8


class ProxyHarvesterConfig(BaseModel):
    sources: list[str] = Field(
        default_factory=lambda: ["proxifly", "proxyscrape", "iplocate", "proxripper"]
    )
    interval_seconds: int = 600


class PolitenessConfig(BaseModel):
    default_concurrency: int = 2
    default_delay_seconds: float = 5.0
    slot_ttl_seconds: int = 120


class CircuitBreakerConfig(BaseModel):
    failure_threshold: float = 0.95
    attempt_threshold: int = 20
    cooldown_seconds: int = 600
    max_cooldown_seconds: int = 3600


class CapSolverConfig(BaseModel):
    daily_credit_ceiling_default: float = 1.0  # [CONFIRMED — BD-03: $1.00/day]
    max_concurrent_solves: int = 10


class SSRFGuardConfig(BaseModel):
    additional_denied_cidrs: list[str] = Field(default_factory=list)


class ObservabilityConfig(BaseModel):
    metrics_enabled: bool = True
    tracing_enabled: bool = True
    logging_level: str = "INFO"


class PgBouncerConfig(BaseModel):
    pool_mode: str = "transaction"
    max_client_conn: int = 500  # [CONFIRMED — BD-06]
    default_pool_size: int = 20


class SessionRetentionConfig(BaseModel):
    browser_sessions_ttl_days: int = 30
    domain_ban_history_retention_days: int = 7


class AppConfig(BaseModel):
    """Root configuration schema matching config/base.yaml."""

    tenant_id: str | None = None  # only for log enrichment (ContextVar), never for routing
    levels: LevelsConfig = Field(default_factory=LevelsConfig)
    camoufox: CamoufoxConfig = Field(default_factory=CamoufoxConfig)
    proxy_harvester: ProxyHarvesterConfig = Field(default_factory=ProxyHarvesterConfig)
    politeness: PolitenessConfig = Field(default_factory=PolitenessConfig)
    circuit_breaker: CircuitBreakerConfig = Field(default_factory=CircuitBreakerConfig)
    capsolver: CapSolverConfig = Field(default_factory=CapSolverConfig)
    ssrf_guard: SSRFGuardConfig = Field(default_factory=SSRFGuardConfig)
    observability: ObservabilityConfig = Field(default_factory=ObservabilityConfig)
    pgbouncer: PgBouncerConfig = Field(default_factory=PgBouncerConfig)
    session_retention: SessionRetentionConfig = Field(default_factory=SessionRetentionConfig)
