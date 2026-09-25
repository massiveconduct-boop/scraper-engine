# tests/integration/test_worker_cache.py
"""Worker._check_cache against real Postgres (round 69)."""

import uuid
from unittest.mock import AsyncMock

import pytest

from scraper_engine.core.tenant import TenantId
from scraper_engine.orchestrator.worker import Worker
from scraper_engine.storage.postgres_client import PostgresClient

TENANT = TenantId("cachetest69")
URL = "https://blocked.example/article"


@pytest.fixture
async def pg():
    client = PostgresClient(
        pgbouncer_dsn="postgresql://scraper:scraper@localhost:5432/scraper_engine",
        pool_size=2,
    )
    await client.start()
    async with client.acquire(TenantId("system")) as conn:
        await conn.execute("SELECT public.create_tenant_schema($1)", str(TENANT))
    async with client.acquire(TENANT) as conn:
        await conn.execute("DELETE FROM scrape_results")
        await conn.execute("DELETE FROM scrape_jobs")
    yield client
    await client.stop()


async def _store(pg, *, status: int, age: str) -> None:
    job_id = str(uuid.uuid4())
    async with pg.acquire(TENANT) as conn:
        await conn.execute(
            "INSERT INTO scrape_jobs (job_id, urls, status) VALUES ($1::uuid, $2, 'COMPLETED')",
            job_id,
            [URL],
        )
        await conn.execute(
            f"""INSERT INTO scrape_results
                  (job_id, url, success, http_status, is_challenge_page, level_used,
                   markdown, extracted_at)
                VALUES ($1::uuid, $2, true, $3, false, 3, $4, NOW() - INTERVAL '{age}')""",
            job_id,
            URL,
            status,
            f"page stored with {status}",
        )


def _worker(pg) -> Worker:
    redis = AsyncMock()
    redis.raw.get.return_value = None
    return Worker(
        redis=redis,
        circuit_breaker=AsyncMock(),
        politeness=AsyncMock(),
        dlq=AsyncMock(),
        pg=pg,
    )


@pytest.mark.asyncio
async def test_a_success_stored_with_a_block_status_is_never_reused(pg):
    """Live: a 401 at L3 was stored as a success whose content was the page
    title. Once stored it would have been served for the whole cache TTL."""
    await _store(pg, status=401, age="1 minute")
    assert await _worker(pg)._check_cache(TENANT, URL) is None


@pytest.mark.asyncio
async def test_an_older_real_success_is_still_reused(pg):
    await _store(pg, status=200, age="1 hour")
    await _store(pg, status=401, age="1 minute")
    cached = await _worker(pg)._check_cache(TENANT, URL)
    assert cached is not None
    assert cached.http_status == 200
    assert cached.markdown == "page stored with 200"
