# tests/integration/test_promotion.py
"""Controlled proxy promotion integration test — exercises ProxyPromotionJob's
own pipeline mechanics (DB writes, scoring, concurrency, attempt tracking),
not real-world external-proxy validation.

Plan §4.4: uses ProxyPromotionJob.run_once() (not the legacy promote_tcp_only).
Deterministic, repeatable — seeds a "proxy" pointing at the local judge_server.py
stand-in and asserts promotion from score 25 → 60.

Important scope note (round 32): this seeds ip=port=the judge's own address,
so the "proxy" and the validation target are the same machine — a degenerate
case real external routing never produces (a real proxy forwards a request
made ON ITS BEHALF to a target it does NOT own). This test cannot and does
not prove that a real third-party proxy validates successfully end to end —
that depends on live, flaky, uncontrollable third-party network behavior and
is checked separately (see tests/live/test_proxy_judge_reachability.py for
the narrower, deterministic piece of that which CAN be asserted: that the
production validation target itself is real and reachable).
"""

import pytest

from scraper_engine.core.tenant import TenantId
from scraper_engine.proxy.harvester import ProxyHarvester
from scraper_engine.proxy.judge_server import start as start_judge_server
from scraper_engine.proxy.promotion import ProxyPromotionJob
from scraper_engine.storage.postgres_client import PostgresClient


@pytest.fixture(scope="module")
def judge_server():
    """Start the real (embedded-thread) judge server on port 8089."""
    server = start_judge_server()
    yield
    server.shutdown()
    server.server_close()


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
    """Seed a "proxy" pointing at the local judge stand-in at score 25 and
    assert it promotes to 60.

    Plan §4.4: uses ProxyPromotionJob.run_once() (the plan's specified implementation).
    This is a controlled, deterministic proof of the promotion PIPELINE's
    mechanics, without flaky dependencies on wild proxies or the real
    internet — see the module docstring for what this does NOT prove.
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
    assert result["promoted"] >= 1, f"Expected at least 1 promoted proxy, got {result}"

    # Fetch updated score
    rows = await pg.fetch(
        tenant,
        "SELECT reliability_score, anonymity_level FROM proxy_pool "
        "WHERE ip = $1 AND port = $2 AND protocol = $3",
        ip,
        port,
        protocol,
    )

    assert len(rows) == 1
    # Round 32: score is now formula-computed via ScoringEngine (real
    # measured latency to the local judge stand-in, ELITE anonymity since
    # judge_server.py sets no Via/XFF/Proxy-Connection headers, UNKNOWN ASN
    # for 127.0.0.1, success_rate=None on a first validation) instead of a
    # flat 60 — bounds-checked rather than an exact float to tolerate
    # latency jitter, but must clear L2's 70 threshold given near-zero
    # loopback latency + elite anonymity.
    assert 70.0 <= rows[0]["reliability_score"] <= 100.0
    assert rows[0]["anonymity_level"] == "elite"

    # Clean up database row
    await pg.execute(
        tenant,
        "DELETE FROM proxy_pool WHERE ip = $1 AND port = $2 AND protocol = $3",
        ip,
        port,
        protocol,
    )
