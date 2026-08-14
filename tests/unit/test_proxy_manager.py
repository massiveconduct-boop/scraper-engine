# tests/unit/test_proxy_manager.py
"""ProxyManager tests — scored selection, domain bans, exhaustion."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from scraper_engine.config.schema import ProxyTierConfig
from scraper_engine.core.exceptions import ProxyPoolExhaustedError
from scraper_engine.core.models import Proxy, ProxyProtocol
from scraper_engine.core.tenant import TenantId
from scraper_engine.proxy.manager import ProxyManager


@pytest.fixture
def tenant():
    return TenantId("test")


@pytest.fixture
def sample_proxies():
    # Sorted by reliability_score DESC as the query would return
    return [
        Proxy(id=3, ip="3.3.3.3", port=8080, protocol=ProxyProtocol.HTTPS, reliability_score=95.0),
        Proxy(id=1, ip="1.1.1.1", port=8080, protocol=ProxyProtocol.HTTP, reliability_score=90.0),
        Proxy(id=2, ip="2.2.2.2", port=8080, protocol=ProxyProtocol.HTTP, reliability_score=80.0),
    ]


def _proxy_row(p: Proxy) -> dict:
    return {
        "id": p.id,
        "ip": p.ip,
        "port": p.port,
        "protocol": p.protocol.value,
        "anonymity_level": p.anonymity_level.value,
        "asn_class": p.asn_class.value,
        "reliability_score": p.reliability_score,
    }


def make_manager(redis_get_return=None):
    redis = AsyncMock()
    redis.get.return_value = redis_get_return  # None = not banned
    redis.set.return_value = None
    pg = AsyncMock()
    pg.fetch.return_value = []
    pg.execute.return_value = "OK"
    return ProxyManager(redis=redis, pg=pg)


class TestProxyManager:
    @pytest.mark.asyncio
    async def test_exhausted_when_pool_empty(self, tenant):
        pm = make_manager()
        with pytest.raises(ProxyPoolExhaustedError) as exc:
            await pm.get_proxy(tenant, level=1, domain="example.com")
        assert exc.value.domain == "example.com"
        assert exc.value.level == 1

    @pytest.mark.asyncio
    async def test_exhaustion_sets_debounced_kick_and_publishes_when_newly_set(self, tenant):
        """round 34 — proxy/harvester_daemon.py's out-of-band harvest is
        triggered by this SET NX; PUBLISH only fires for the caller that
        actually creates the key, not every exhausted request."""
        from scraper_engine.proxy.manager import HARVEST_KICK_CHANNEL, HARVEST_KICK_KEY

        pm = make_manager()
        pm._redis.raw.set.return_value = True  # key did not already exist

        with pytest.raises(ProxyPoolExhaustedError):
            await pm.get_proxy(tenant, level=1, domain="example.com")

        pm._redis.raw.set.assert_awaited_once_with(HARVEST_KICK_KEY, "1", nx=True, ex=30)
        pm._redis.raw.publish.assert_awaited_once_with(HARVEST_KICK_CHANNEL, "1")

    @pytest.mark.asyncio
    async def test_exhaustion_does_not_publish_when_kick_already_pending(self, tenant):
        """Debounce: a kick already pending (SET NX returns falsy) means
        another exhausted request already triggered the signal — this one
        must not also publish, or a stampede of exhausted requests would
        stampede the harvester with redundant triggers."""
        pm = make_manager()
        pm._redis.raw.set.return_value = False  # key already existed

        with pytest.raises(ProxyPoolExhaustedError):
            await pm.get_proxy(tenant, level=1, domain="example.com")

        pm._redis.raw.set.assert_awaited_once()
        pm._redis.raw.publish.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_exhaustion_signal_failure_does_not_fail_the_fetch(self, tenant):
        """Best-effort: a Redis hiccup while signaling must surface as the
        real ProxyPoolExhaustedError, not an unrelated exception from the
        signaling side-channel."""
        pm = make_manager()
        pm._redis.raw.set.side_effect = RuntimeError("redis down")

        with pytest.raises(ProxyPoolExhaustedError):
            await pm.get_proxy(tenant, level=1, domain="example.com")

    @pytest.mark.asyncio
    async def test_selects_from_pool(self, tenant, sample_proxies):
        redis = AsyncMock()
        redis.get.return_value = None
        pg = AsyncMock()
        pg.fetch.return_value = [
            {
                "id": p.id,
                "ip": p.ip,
                "port": p.port,
                "protocol": p.protocol.value,
                "anonymity_level": p.anonymity_level.value,
                "asn_class": p.asn_class.value,
                "reliability_score": p.reliability_score,
            }
            for p in sample_proxies
        ]
        pm = ProxyManager(redis=redis, pg=pg)
        lease = await pm.get_proxy(tenant, level=1, domain="example.com")
        assert lease.proxy.ip == "3.3.3.3"  # highest score

    @pytest.mark.asyncio
    async def test_skips_banned_proxies(self, tenant, sample_proxies):
        redis = AsyncMock()
        # First two are banned, third is not
        redis.get.side_effect = ["1", "1", None]
        pg = AsyncMock()
        pg.fetch.return_value = [
            {
                "id": p.id,
                "ip": p.ip,
                "port": p.port,
                "protocol": p.protocol.value,
                "anonymity_level": p.anonymity_level.value,
                "asn_class": p.asn_class.value,
                "reliability_score": p.reliability_score,
            }
            for p in sample_proxies
        ]
        pm = ProxyManager(redis=redis, pg=pg)
        lease = await pm.get_proxy(tenant, level=2, domain="example.com")
        assert lease.proxy.ip == "2.2.2.2"  # 3.3.3.3 and 1.1.1.1 are banned

    @pytest.mark.asyncio
    async def test_exhausted_when_all_candidates_stay_banned(self, tenant):
        """Every candidate found across MAX_ATTEMPTS retries is domain-banned
        (never None) — the loop must fall through and raise after exhausting
        attempts, distinct from the pool-empty (proxy is None) exhaustion path."""
        proxies = [
            Proxy(
                id=i,
                ip=f"{i}.{i}.{i}.{i}",
                port=8080,
                protocol=ProxyProtocol.HTTP,
                reliability_score=90.0,
            )
            for i in range(1, ProxyManager.MAX_ATTEMPTS + 1)
        ]
        redis = AsyncMock()
        redis.get.return_value = "1"  # always banned
        pg = AsyncMock()
        pg.fetch.return_value = [
            {
                "id": p.id,
                "ip": p.ip,
                "port": p.port,
                "protocol": p.protocol.value,
                "anonymity_level": p.anonymity_level.value,
                "asn_class": p.asn_class.value,
                "reliability_score": p.reliability_score,
            }
            for p in proxies
        ]
        pm = ProxyManager(redis=redis, pg=pg)

        with pytest.raises(ProxyPoolExhaustedError) as exc:
            await pm.get_proxy(tenant, level=1, domain="example.com")
        assert exc.value.attempts == ProxyManager.MAX_ATTEMPTS
        redis.raw.incr.assert_awaited_once_with("metrics:proxy_exhausted_total:1")

    @pytest.mark.asyncio
    async def test_mark_success(self, tenant):
        """Round 32: mark_success now recomputes reliability_score via
        ScoringEngine from real accumulated history instead of a flat +5 —
        assert the RETURNING row is read and a formula-consistent score is
        written back, not just "doesn't raise"."""
        redis = AsyncMock()
        pg = AsyncMock()
        pg.fetchrow.return_value = {
            "anonymity_level": "elite",
            "asn_class": "residential",
            "response_time_ms": 50,
            "global_success_count": 2,
            "global_failure_count": 0,
            "last_validated": datetime.now(UTC),
        }
        pm = ProxyManager(redis=redis, pg=pg)

        await pm.mark_success(tenant, "1.2.3.4", 8080)

        pg.fetchrow.assert_awaited_once()
        pg.execute.assert_awaited_once()
        args = pg.execute.await_args.args  # (tenant_id, sql, score, ip, port)
        assert args[1].strip().startswith("UPDATE proxy_pool SET reliability_score")
        new_score = args[2]
        assert 0.0 <= new_score <= 100.0
        # 2 successes / 0 failures -> success_rate=100, elite+residential ->
        # should land well above L2's 70 threshold.
        assert new_score >= 70.0

    @pytest.mark.asyncio
    async def test_mark_success_no_matching_row_is_a_noop(self, tenant):
        """A proxy that no longer exists in the pool (e.g. reaped between
        lease and use) must not crash — fetchrow returning None short-circuits."""
        redis = AsyncMock()
        pg = AsyncMock()
        pg.fetchrow.return_value = None
        pm = ProxyManager(redis=redis, pg=pg)

        await pm.mark_success(tenant, "1.2.3.4", 8080)

        pg.execute.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_mark_failure(self, tenant):
        """Round 32: mark_failure now recomputes via ScoringEngine instead
        of a flat -10 — a proxy with zero successes and real failures
        should land well below L1's 40 threshold."""
        redis = AsyncMock()
        pg = AsyncMock()
        pg.fetchrow.return_value = {
            "anonymity_level": "transparent",
            "asn_class": "unknown",
            "response_time_ms": 500,
            "global_success_count": 0,
            "global_failure_count": 3,
            "last_validated": datetime.now(UTC),
        }
        pm = ProxyManager(redis=redis, pg=pg)

        await pm.mark_failure(tenant, "1.2.3.4", 8080, "example.com")

        redis.set.assert_awaited_once()
        pg.fetchrow.assert_awaited_once()
        pg.execute.assert_awaited_once()
        new_score = pg.execute.await_args.args[2]  # (tenant_id, sql, score, ip, port)
        assert 0.0 <= new_score <= 100.0
        assert new_score < 40.0

    @pytest.mark.asyncio
    async def test_mark_failure_no_matching_row_is_a_noop(self, tenant):
        redis = AsyncMock()
        pg = AsyncMock()
        pg.fetchrow.return_value = None
        pm = ProxyManager(redis=redis, pg=pg)

        await pm.mark_failure(tenant, "1.2.3.4", 8080, "example.com")

        redis.set.assert_awaited_once()  # ban is set before the DB lookup
        pg.execute.assert_not_awaited()


class TestTier2FallbackForTier3:
    """Round 33: allow_tier2_fallback_for_tier3 is a config-gated stopgap for
    free-only proxy sources where a real 90+-scored (tier-3) proxy can be
    structurally unreachable — see config/base.yaml's proxy_tiers section."""

    @pytest.mark.asyncio
    async def test_disabled_by_default_stays_exhausted_when_no_tier3_proxy(self, tenant):
        """Default config (allow_tier2_fallback_for_tier3=False) — no
        behavior change from before this feature existed."""
        redis = AsyncMock()
        redis.get.return_value = None
        pg = AsyncMock()
        pg.fetch.return_value = []  # no proxy scores >= 90
        pm = ProxyManager(redis=redis, pg=pg)  # default ProxyTierConfig()

        with pytest.raises(ProxyPoolExhaustedError):
            await pm.get_proxy(tenant, level=3, domain="example.com")

    @pytest.mark.asyncio
    async def test_enabled_falls_back_to_tier2_proxy_when_tier3_empty(self, tenant):
        tier2_proxy = Proxy(
            id=1, ip="7.7.7.7", port=8080, protocol=ProxyProtocol.HTTP, reliability_score=75.0
        )
        redis = AsyncMock()
        redis.get.return_value = None
        pg = AsyncMock()
        # First call (min_score=90) finds nothing; the fallback retry
        # (min_score=70) finds the tier-2-caliber proxy.
        pg.fetch.side_effect = [[], [_proxy_row(tier2_proxy)]]
        pm = ProxyManager(
            redis=redis,
            pg=pg,
            tier_config=ProxyTierConfig(allow_tier2_fallback_for_tier3=True),
        )

        lease = await pm.get_proxy(tenant, level=3, domain="example.com")

        assert lease.proxy.ip == "7.7.7.7"
        assert pg.fetch.await_count == 2

    @pytest.mark.asyncio
    async def test_enabled_prefers_real_tier3_proxy_when_available(self, tenant):
        """The fallback must never skip searching for a genuine tier-3
        proxy first — only tried after that search comes up empty."""
        tier3_proxy = Proxy(
            id=1, ip="9.9.9.9", port=8080, protocol=ProxyProtocol.HTTP, reliability_score=95.0
        )
        redis = AsyncMock()
        redis.get.return_value = None
        pg = AsyncMock()
        pg.fetch.return_value = [_proxy_row(tier3_proxy)]
        pm = ProxyManager(
            redis=redis,
            pg=pg,
            tier_config=ProxyTierConfig(allow_tier2_fallback_for_tier3=True),
        )

        lease = await pm.get_proxy(tenant, level=3, domain="example.com")

        assert lease.proxy.ip == "9.9.9.9"
        pg.fetch.assert_awaited_once()  # never needed the fallback retry

    @pytest.mark.asyncio
    async def test_enabled_stays_exhausted_when_no_proxy_at_any_tier(self, tenant):
        redis = AsyncMock()
        redis.get.return_value = None
        pg = AsyncMock()
        pg.fetch.return_value = []  # empty at both min_score=90 and min_score=70
        pm = ProxyManager(
            redis=redis,
            pg=pg,
            tier_config=ProxyTierConfig(allow_tier2_fallback_for_tier3=True),
        )

        with pytest.raises(ProxyPoolExhaustedError):
            await pm.get_proxy(tenant, level=3, domain="example.com")
        assert pg.fetch.await_count == 2

    @pytest.mark.asyncio
    async def test_fallback_does_not_apply_to_level_2(self, tenant):
        """The config flag is explicitly tier3-specific — level 2 exhausting
        must not trigger any fallback, even with the flag enabled."""
        redis = AsyncMock()
        redis.get.return_value = None
        pg = AsyncMock()
        pg.fetch.return_value = []
        pm = ProxyManager(
            redis=redis,
            pg=pg,
            tier_config=ProxyTierConfig(allow_tier2_fallback_for_tier3=True),
        )

        with pytest.raises(ProxyPoolExhaustedError):
            await pm.get_proxy(tenant, level=2, domain="example.com")
        pg.fetch.assert_awaited_once()  # no fallback retry attempted
