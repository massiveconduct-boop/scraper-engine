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
        from storage.postgres_client import PostgresClient
        from storage.redis_client import RedisClient

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

        yield

        if deps._storage_pg is not None:
            await deps._storage_pg.stop()
        if deps._storage_redis is not None:
            await deps._storage_redis.stop()

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
