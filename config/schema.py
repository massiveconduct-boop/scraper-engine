# config/schema.py
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator


class LevelConfig(BaseModel):
    # Round 25: Botasaurus is real again (fetcher/factory.py constructs a
    # BotasaurusWrapper whenever "botasaurus" appears in this value) — L2
    # tries it first, falling back to Camoufox on failure or a detected
    # challenge page. "camoufox" alone is still valid: it skips the
    # Botasaurus attempt entirely, same as L3.
    engine: Literal["scrapling", "camoufox", "botasaurus+camoufox"]
    proxy_tier_min_score: float
    timeout_seconds: int
    capsolver_enabled: bool = False
    # L2/L3 wait strategy — config-driven, not hardcoded (round 12.1)
    goto_wait_until: str = "load"
    networkidle_timeout_ms: int = 5000
    max_total_wait_ms: int = 30000
    post_load_fixed_wait_ms: int = 10000
    retry_wait_increment_ms: int = 5000
    # Lazy-load / infinite-scroll (round 15 follow-up). scroll_passes=0 disables;
    # >0 scrolls to bottom up to N times, early-exiting when height stops growing.
    scroll_passes: int = 0
    scroll_wait_ms: int = 1500


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


class BotasaurusConfig(BaseModel):
    """Round 26 capability-upgrade knobs — every field here maps to a real,
    verified botasaurus/botasaurus_driver kwarg or call (verified against the
    installed 4.0.97/4.0.93/4.0.38 source, not just the README — see
    .claude/knowledge/architecture.md -> "Botasaurus Integration").

    Defaults turn on the strict upgrades over the round-25 baseline
    (bypass_cloudflare, tiny_profile, the anti-detection args, hashed
    fingerprint pairing) since they're free wins with no behavior regression.
    max_retry defaults to 0 (off) to keep today's single-attempt behavior
    unless explicitly opted into. l1_ja3_client_enabled defaults to False —
    a brand-new L1 code path with no live-traffic validation yet.
    """

    bypass_cloudflare: bool = True
    tiny_profile: bool = True
    remove_default_browser_check_argument: bool = True
    close_on_crash: bool = True
    random_sleep_enabled: bool = True
    hashed_fingerprint: bool = True
    max_retry: int = 0
    l1_ja3_client_enabled: bool = False


class ProxyHarvesterConfig(BaseModel):
    sources: list[str] = Field(
        default_factory=lambda: ["proxifly", "proxyscrape", "iplocate", "proxripper"]
    )
    interval_seconds: int = 600
    # The daemon runs three loops on independent timers (proxy/harvester_daemon.py).
    promotion_interval_seconds: int = 900
    health_interval_seconds: int = 300


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
    # Per-tenant ceiling default lives in the `tenants.capsolver_daily_credit_ceiling`
    # DB column (round 25) — not duplicated here to avoid two conflicting sources
    # of truth. CapSolverBudget falls back to its own DEFAULT_DAILY_CEILING ($1.00)
    # only when no tenant row / pg client is available.
    max_concurrent_solves: int = 10


class SSRFGuardConfig(BaseModel):
    additional_denied_cidrs: list[str] = Field(default_factory=list)


class ObservabilityConfig(BaseModel):
    metrics_enabled: bool = True
    tracing_enabled: bool = True
    logging_level: str = "INFO"
    # OTLPSpanExporter's own default (localhost:4317) resolves inside whichever
    # container is exporting, never reaching the separate jaeger service — this
    # is the single source of truth every process points at instead.
    otlp_endpoint: str = "http://jaeger:4317"


class PgBouncerConfig(BaseModel):
    """Informational only (round 25) — nothing reads these fields at runtime.

    The real PgBouncer process is configured entirely by the static
    infra/pgbouncer/pgbouncer.ini file plus docker-compose.yml env vars.
    Editing base.yaml's pgbouncer: section has zero effect on the deployed
    pooler; these values exist to document what pgbouncer.ini is set to, kept
    in sync by hand. Templating pgbouncer.ini from this config would be the
    real fix, but that's a deploy-tooling change, out of scope here."""

    pool_mode: str = "transaction"
    max_client_conn: int = 500  # [CONFIRMED — BD-06]
    default_pool_size: int = 20


class SessionRetentionConfig(BaseModel):
    browser_sessions_ttl_days: int = 30
    domain_ban_history_retention_days: int = 7
    cleanup_interval_seconds: int = 3600


class StorageConfig(BaseModel):
    """Single source of truth for the database and Redis connection strings.

    The application connects to Postgres with raw asyncpg, which needs a plain
    ``postgresql://`` DSN. Alembic/SQLAlchemy (in alembic.ini) use the
    ``postgresql+asyncpg://`` form instead — a different consumer with a
    different format. The validator below strips any ``+driver`` suffix so a
    value written in the SQLAlchemy form still works for asyncpg here.

    Defaults point at the docker-compose service names and route the database
    through PgBouncer (invariant G-05). Override per environment via the
    ``${DATABASE_URL}`` / ``${REDIS_URL}`` placeholders in base.yaml.
    """

    database_url: str = "postgresql://scraper:scraper@pgbouncer:6432/scraper_engine"
    redis_url: str = "redis://redis:6379/0"

    @field_validator("database_url")
    @classmethod
    def _strip_sqlalchemy_driver(cls, v: str) -> str:
        # asyncpg.create_pool rejects the SQLAlchemy "postgresql+asyncpg://" form;
        # normalise it down to the plain scheme asyncpg expects.
        if v.startswith("postgresql+"):
            return "postgresql://" + v.split("://", 1)[1]
        return v


class S3Config(BaseModel):
    """Object storage for HTML snapshots (scrape_results.html_snapshot_url).

    Defaults point at the docker-compose MinIO service. Override per
    environment via the ``${S3_*}`` placeholders in base.yaml.
    """

    endpoint_url: str = "http://minio:9000"
    access_key: str = "minioadmin"
    secret_key: str = "minioadmin"
    bucket: str = "scraper-snapshots"


class AppConfig(BaseModel):
    """Root configuration schema matching config/base.yaml."""

    tenant_id: str | None = None  # only for log enrichment (ContextVar), never for routing
    storage: StorageConfig = Field(default_factory=StorageConfig)
    s3: S3Config = Field(default_factory=S3Config)
    levels: LevelsConfig = Field(default_factory=LevelsConfig)
    camoufox: CamoufoxConfig = Field(default_factory=CamoufoxConfig)
    botasaurus: BotasaurusConfig = Field(default_factory=BotasaurusConfig)
    proxy_harvester: ProxyHarvesterConfig = Field(default_factory=ProxyHarvesterConfig)
    politeness: PolitenessConfig = Field(default_factory=PolitenessConfig)
    circuit_breaker: CircuitBreakerConfig = Field(default_factory=CircuitBreakerConfig)
    capsolver: CapSolverConfig = Field(default_factory=CapSolverConfig)
    ssrf_guard: SSRFGuardConfig = Field(default_factory=SSRFGuardConfig)
    observability: ObservabilityConfig = Field(default_factory=ObservabilityConfig)
    pgbouncer: PgBouncerConfig = Field(default_factory=PgBouncerConfig)
    session_retention: SessionRetentionConfig = Field(default_factory=SessionRetentionConfig)
