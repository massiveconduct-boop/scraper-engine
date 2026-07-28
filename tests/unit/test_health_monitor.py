# tests/unit/test_health_monitor.py
"""HealthMonitor tests — proxy validation with mocked DB + Redis."""

from unittest.mock import AsyncMock, patch

import pytest

from scraper_engine.proxy.health_monitor import HealthMonitor


@pytest.fixture
def pg():
    pg = AsyncMock()
    pg.fetch.return_value = [{"ip": "1.2.3.4", "port": 8080}]
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
        with patch.object(hm, "check_one", return_value=True):
            result = await hm.check_all()
            assert result["validated"] == 1
            assert result["downgraded"] == 0

    @pytest.mark.asyncio
    async def test_check_all_downgrades(self, pg, redis):
        hm = HealthMonitor(pg=pg, redis=redis)
        with patch.object(hm, "check_one", return_value=False):
            result = await hm.check_all()
            assert result["validated"] == 0
            assert result["downgraded"] == 1

    @pytest.mark.asyncio
    async def test_check_all_writes_pool_size_metric(self, pg, redis):
        """api/health.py reads metrics:proxy_pool_size for GET /health's
        proxy_pool_size — this was previously never written anywhere."""
        hm = HealthMonitor(pg=pg, redis=redis)
        with patch.object(hm, "check_one", return_value=True):
            await hm.check_all()
        redis.set.assert_awaited_once()
        args, kwargs = redis.set.await_args
        assert args[1] == "metrics:proxy_pool_size"
        assert args[2] == "1"

    @pytest.mark.asyncio
    async def test_check_one_returns_bool(self, pg, redis):
        hm = HealthMonitor(pg=pg, redis=redis)
        with patch("httpx.AsyncClient.get") as mock_get:
            mock_get.return_value.status_code = 200
            result = await hm.check_one("1.2.3.4", 8080)
            assert result is True

    @pytest.mark.asyncio
    async def test_check_one_failure(self, pg, redis):
        hm = HealthMonitor(pg=pg, redis=redis)
        with patch("httpx.AsyncClient.get", side_effect=OSError("refused")):
            result = await hm.check_one("1.2.3.4", 8080)
            assert result is False
