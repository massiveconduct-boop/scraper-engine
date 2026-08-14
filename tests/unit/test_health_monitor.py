# tests/unit/test_health_monitor.py
"""HealthMonitor tests — proxy validation with mocked DB + Redis."""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from scraper_engine.core.models import AnonymityLevel, AsnClass
from scraper_engine.proxy.health_monitor import HealthMonitor


@pytest.fixture
def pg():
    pg = AsyncMock()
    pg.fetch.return_value = [
        {
            "ip": "1.2.3.4",
            "port": 8080,
            "protocol": "HTTP",
            "global_success_count": 0,
            "global_failure_count": 0,
        }
    ]
    pg.execute.return_value = "DELETE 0"
    pg.fetchrow.return_value = {"n": 1}
    return pg


@pytest.fixture
def redis():
    redis = AsyncMock()
    redis.get.return_value = None
    return redis


class TestHealthMonitor:
    def test_init(self, pg, redis):
        hm = HealthMonitor(pg=pg, redis=redis)
        assert hm._pg is pg

    @pytest.mark.asyncio
    async def test_check_all_validates(self, pg, redis):
        hm = HealthMonitor(pg=pg, redis=redis)
        with patch.object(
            hm, "check_one", return_value=(True, AnonymityLevel.ELITE, AsnClass.UNKNOWN, 50)
        ):
            result = await hm.check_all()
            assert result["validated"] == 1
            assert result["downgraded"] == 0

    @pytest.mark.asyncio
    async def test_check_all_rescoring_uses_fresh_reading(self, pg, redis):
        """Round 38: a passing validation must UPDATE anonymity_level/
        asn_class/response_time_ms/reliability_score from the fresh
        check_one() reading, not just bump last_validated."""
        hm = HealthMonitor(pg=pg, redis=redis)
        with patch.object(
            hm,
            "check_one",
            return_value=(True, AnonymityLevel.ELITE, AsnClass.RESIDENTIAL, 40),
        ):
            await hm.check_all()
        update_call = next(
            c for c in pg.execute.await_args_list if "SET anonymity_level" in c.args[1]
        )
        assert update_call.args[2] == AnonymityLevel.ELITE.value
        assert update_call.args[3] == AsnClass.RESIDENTIAL.value
        assert update_call.args[4] == 40

    @pytest.mark.asyncio
    async def test_check_all_downgrades(self, pg, redis):
        hm = HealthMonitor(pg=pg, redis=redis)
        with patch.object(
            hm,
            "check_one",
            return_value=(False, AnonymityLevel.TRANSPARENT, AsnClass.UNKNOWN, None),
        ):
            result = await hm.check_all()
            assert result["validated"] == 0
            assert result["downgraded"] == 1

    @pytest.mark.asyncio
    async def test_check_all_downgrade_refreshes_last_validated(self, pg, redis):
        """Regression: a failed check must still bump last_validated, or the
        rolling ORDER BY last_validated ASC LIMIT 100 coverage design gets
        stuck re-picking the same failing rows forever, starving the rest
        of the pool of ever being re-checked. Confirmed live: 61% of a real
        pool went over an hour untouched despite the cycle running every
        5-8 minutes, because failures never advanced their timestamp."""
        hm = HealthMonitor(pg=pg, redis=redis)
        with patch.object(
            hm,
            "check_one",
            return_value=(False, AnonymityLevel.TRANSPARENT, AsnClass.UNKNOWN, None),
        ):
            await hm.check_all()
        downgrade_call = next(
            c for c in pg.execute.await_args_list if "reliability_score - 20.0" in c.args[1]
        )
        assert "last_validated = NOW()" in downgrade_call.args[1]

    @pytest.mark.asyncio
    async def test_check_all_writes_pool_size_metric(self, pg, redis):
        """api/health.py reads metrics:proxy_pool_size for GET /health's
        proxy_pool_size — this was previously never written anywhere."""
        hm = HealthMonitor(pg=pg, redis=redis)
        with patch.object(
            hm, "check_one", return_value=(True, AnonymityLevel.ELITE, AsnClass.UNKNOWN, 50)
        ):
            await hm.check_all()
        redis.set.assert_awaited_once()
        args, kwargs = redis.set.await_args
        assert args[1] == "metrics:proxy_pool_size"
        assert args[2] == "1"

    @pytest.mark.asyncio
    async def test_check_one_delegates_to_http_validate(self, pg, redis):
        """check_one now delegates to ProxyHarvester._http_validate (round
        38) instead of a hand-rolled duplicate JUDGE_URLS loop, so both
        modules share one validation + accurate-latency implementation."""
        with patch(
            "scraper_engine.proxy.health_monitor.ProxyHarvester._http_validate",
            AsyncMock(return_value=(True, AnonymityLevel.ELITE, 42)),
        ):
            hm = HealthMonitor(pg=pg, redis=redis)
            is_valid, anonymity, asn, latency_ms = await hm.check_one("1.2.3.4", 8080, "HTTP")
        assert is_valid is True
        assert anonymity == AnonymityLevel.ELITE
        assert asn == AsnClass.UNKNOWN  # NullAsnClassifier default when none injected
        assert latency_ms == 42

    @pytest.mark.asyncio
    async def test_check_one_failure_returns_none_latency(self, pg, redis):
        with patch(
            "scraper_engine.proxy.health_monitor.ProxyHarvester._http_validate",
            AsyncMock(return_value=(False, AnonymityLevel.TRANSPARENT, None)),
        ):
            hm = HealthMonitor(pg=pg, redis=redis)
            is_valid, anonymity, asn, latency_ms = await hm.check_one("1.2.3.4", 8080, "HTTP")
        assert is_valid is False
        assert anonymity == AnonymityLevel.TRANSPARENT
        assert asn == AsnClass.UNKNOWN
        assert latency_ms is None

    @pytest.mark.asyncio
    async def test_check_all_bounds_concurrency(self, pg, redis):
        """Round 38 regression: check_all must not validate all rows fully
        sequentially or fully unbounded — must respect HEALTH_CHECK_CONCURRENCY.
        Confirmed live: sequential validation (judge round-trip + a DNS
        classify() call per row) turned a 100-row cycle into 15-25+ minutes
        against a configured 300s interval before this fix."""
        from scraper_engine.proxy.health_monitor import HEALTH_CHECK_CONCURRENCY

        pg.fetch.return_value = [
            {
                "ip": f"1.2.3.{i}",
                "port": 8080,
                "protocol": "HTTP",
                "global_success_count": 0,
                "global_failure_count": 0,
            }
            for i in range(20)
        ]
        hm = HealthMonitor(pg=pg, redis=redis)
        in_flight = {"current": 0, "max": 0}

        async def slow_check_one(ip, port, protocol="HTTP"):
            in_flight["current"] += 1
            in_flight["max"] = max(in_flight["max"], in_flight["current"])
            await asyncio.sleep(0.01)
            in_flight["current"] -= 1
            return True, AnonymityLevel.ELITE, AsnClass.UNKNOWN, 50

        with patch.object(hm, "check_one", slow_check_one):
            await hm.check_all()
        assert in_flight["max"] <= HEALTH_CHECK_CONCURRENCY
        assert in_flight["max"] > 1, "test is meaningless if nothing ran concurrently"

    @pytest.mark.asyncio
    async def test_check_one_uses_injected_classifier(self, pg, redis):
        classifier = AsyncMock()
        classifier.classify.return_value = "residential"
        with patch(
            "scraper_engine.proxy.health_monitor.ProxyHarvester._http_validate",
            AsyncMock(return_value=(True, AnonymityLevel.ELITE, 42)),
        ):
            hm = HealthMonitor(pg=pg, redis=redis, asn_classifier=classifier)
            _is_valid, _anonymity, asn, _latency_ms = await hm.check_one("1.2.3.4", 8080, "HTTP")
        assert asn == AsnClass.RESIDENTIAL
        classifier.classify.assert_awaited_once_with("1.2.3.4")

    @pytest.mark.asyncio
    async def test_run_forever_logs_and_loops(self, pg, redis, monkeypatch):
        hm = HealthMonitor(pg=pg, redis=redis)

        async def fake_sleep(_):  # break the infinite loop after one cycle
            raise asyncio.CancelledError

        monkeypatch.setattr("scraper_engine.proxy.health_monitor.asyncio.sleep", fake_sleep)
        with (
            patch.object(
                hm, "check_one", return_value=(True, AnonymityLevel.ELITE, AsnClass.UNKNOWN, 50)
            ),
            pytest.raises(asyncio.CancelledError),
        ):
            await hm.run_forever(interval_seconds=1)

    @pytest.mark.asyncio
    async def test_run_forever_swallows_cycle_error_and_keeps_looping(self, pg, redis, monkeypatch):
        """A single failed check_all() cycle must not take run_forever offline —
        matches the daemon supervisor's own isolate-failures contract."""
        hm = HealthMonitor(pg=pg, redis=redis)

        async def fake_sleep(_):
            raise asyncio.CancelledError

        monkeypatch.setattr("scraper_engine.proxy.health_monitor.asyncio.sleep", fake_sleep)
        with (
            patch.object(hm, "check_all", side_effect=RuntimeError("transient boom")),
            pytest.raises(asyncio.CancelledError),
        ):
            await hm.run_forever(interval_seconds=1)
