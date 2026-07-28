# api/main.py
"""FastAPI application entry point."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI


def create_app() -> FastAPI:
    """Build and return the FastAPI application."""
    from scraper_engine.config.loader import load_config
    from scraper_engine.observability.bootstrap import bootstrap_observability

    from .middleware import configure_middleware
    from .routes import register_routes

    cfg = load_config()
    bootstrap_observability(cfg.observability)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        """Connect Postgres, Redis, and initialise TenantResolver at startup."""
        import scraper_engine.api.dependencies as deps
        from scraper_engine.api.auth import TenantResolver
        from scraper_engine.core.ssrf_guard import SSRFGuard
        from scraper_engine.orchestrator.job_queue import build_queue
        from scraper_engine.storage.postgres_client import PostgresClient
        from scraper_engine.storage.redis_client import RedisClient
        from scraper_engine.storage.s3_client import S3Client

        if deps._ssrf_guard is None:
            deps._ssrf_guard = SSRFGuard(cfg.ssrf_guard.additional_denied_cidrs)

        if deps._storage_pg is None:
            # DB traffic goes through PgBouncer (invariant G-05) via the single
            # configured DSN — no hardcoded connection string, no pooler bypass.
            pg = PostgresClient(cfg.storage.database_url, pool_size=2)
            await pg.start()
            deps._storage_pg = pg
            deps._tenant_resolver = TenantResolver(pg=pg)

        if deps._storage_redis is None:
            redis = RedisClient(redis_url=cfg.storage.redis_url)
            await redis.start()
            deps._storage_redis = redis

        if deps._storage_s3 is None:
            s3 = S3Client(
                endpoint_url=cfg.s3.endpoint_url,
                access_key=cfg.s3.access_key,
                secret_key=cfg.s3.secret_key,
                bucket=cfg.s3.bucket,
            )
            await s3.start()
            deps._storage_s3 = s3

        if deps._queue is None:
            deps._queue = build_queue(cfg.storage.redis_url)

        yield

        if deps._storage_pg is not None:
            await deps._storage_pg.stop()
        if deps._storage_redis is not None:
            await deps._storage_redis.stop()
        if deps._storage_s3 is not None:
            await deps._storage_s3.stop()

    app = FastAPI(
        title="Scraper Engine",
        version="0.1.0",
        lifespan=lifespan,
        description="Multi-level web scraping with anti-detection + proxy management",
    )
    configure_middleware(app)
    register_routes(app, cfg)

    if cfg.observability.tracing_enabled:
        # A configured TracerProvider (bootstrap_observability, above) with
        # nothing creating spans is tracing in name only — this is what
        # actually emits a span per request.
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

        FastAPIInstrumentor.instrument_app(app)

    return app


app = create_app()
