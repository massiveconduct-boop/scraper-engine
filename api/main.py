# api/main.py
"""FastAPI application entry point."""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI


def create_app() -> FastAPI:
    """Build and return the FastAPI application."""
    from .middleware import configure_middleware
    from .routes import register_routes

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        """Connect Postgres, Redis, and initialise TenantResolver at startup."""
        import api.dependencies as deps
        from api.auth import TenantResolver
        from storage.postgres_client import PostgresClient
        from storage.redis_client import RedisClient

        if deps._storage_pg is None:
            pg = PostgresClient(
                "postgresql://scraper:scraper@postgres:5432/scraper_engine",
                pool_size=2,
            )
            await pg.start()
            deps._storage_pg = pg
            deps._tenant_resolver = TenantResolver(pg=pg)

        if deps._storage_redis is None:
            redis = RedisClient(redis_url="redis://redis:6379/0")
            await redis.start()
            deps._storage_redis = redis

        yield

        if deps._storage_pg is not None:
            await deps._storage_pg.stop()
        if deps._storage_redis is not None:
            await deps._storage_redis.close()

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
