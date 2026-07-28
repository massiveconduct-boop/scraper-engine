# api/main.py
"""FastAPI application entry point."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI


def create_app() -> FastAPI:
    """Build and return the FastAPI application."""
    from .middleware import configure_middleware
    from .routes import register_routes

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        """Connect Postgres, Redis, and initialise TenantResolver at startup."""
        import api.dependencies as deps
        from api.auth import TenantResolver
        from config.loader import load_config
        from orchestrator.job_queue import build_queue
        from storage.postgres_client import PostgresClient
        from storage.redis_client import RedisClient
        from storage.s3_client import S3Client

        cfg = load_config()

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
    register_routes(app)
    return app


app = create_app()
