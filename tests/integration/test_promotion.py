# tests/integration/test_promotion.py
"""Controlled proxy promotion integration test — validates judge-seeded proxy promotion.

Plan §4.4: uses ProxyPromotionJob.run_once() (not the legacy promote_tcp_only).
Deterministic, repeatable — seeds a proxy pointing at the judge server and
asserts promotion from score 25 → 60.
"""

import subprocess
import time

import pytest

from core.tenant import TenantId
from proxy.harvester import ProxyHarvester
from proxy.promotion import ProxyPromotionJob
from storage.postgres_client import PostgresClient


@pytest.fixture(scope="module")
def judge_server():
    """Start the self-hosted judge server on port 8089 in the background."""
    p = subprocess.Popen(["python", "judge_server.py"])
    # Give the server a moment to bind and start listening
    time.sleep(1.0)
    yield
    p.terminate()
    try:
        p.wait(timeout=2.0)
    except subprocess.TimeoutExpired:
        p.kill()


@pytest.fixture
async def pg():
    client = PostgresClient(
        pgbouncer_dsn="postgresql://scraper:scraper@localhost:5432/scraper_engine",
        pool_size=5,
    )
    await client.start()
    yield client
    await client.stop()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_promote_tcp_only_promotes_seeded_proxy(pg, judge_server):
    """Seed a proxy pointing to the judge server at score 25 and assert it promotes to 60.

    Plan §4.4: uses ProxyPromotionJob.run_once() (the plan's specified implementation).
    This is a controlled, deterministic proof of proxy promotion without flaky
    dependencies on wild proxies.
    """
    tenant = TenantId("system")
    ip = "127.0.0.1"
    port = 8089
    protocol = "HTTP"

    # Clean up all existing proxy records to avoid slow sequential validation of dead proxies
    await pg.execute(tenant, "DELETE FROM proxy_pool")

    # Seed the TCP-only proxy (score = 25) pointing to our judge server
    await pg.execute(
        tenant,
        """
        INSERT INTO proxy_pool (ip, port, protocol, anonymity_level, asn_class, reliability_score)
        VALUES ($1, $2, $3, $4, $5, $6)
        """,
        ip,
        port,
        protocol,
        "transparent",
        "unknown",
        25,
    )

    # Plan §4.4: use ProxyPromotionJob.run_once() — the production code path
    promotion = ProxyPromotionJob(
        pg=pg,
        http_validate_fn=ProxyHarvester._http_validate,
        system_tenant=tenant,
    )
    result = await promotion.run_once()

    # Plan §4.4: do NOT require nonzero promoted count as pass condition
    # for wild-proxy tests. For this controlled judge test, assert promotion.
    assert result["promoted"] >= 1, (
        f"Expected at least 1 promoted proxy, got {result}"
    )

    # Fetch updated score
    rows = await pg.fetch(
        tenant,
        "SELECT reliability_score, anonymity_level FROM proxy_pool WHERE ip = $1 AND port = $2 AND protocol = $3",
        ip,
        port,
        protocol,
    )

    assert len(rows) == 1
    assert rows[0]["reliability_score"] == 60.0
    assert rows[0]["anonymity_level"] == "elite"

    # Clean up database row
    await pg.execute(
        tenant,
        "DELETE FROM proxy_pool WHERE ip = $1 AND port = $2 AND protocol = $3",
        ip,
        port,
        protocol,
    )
