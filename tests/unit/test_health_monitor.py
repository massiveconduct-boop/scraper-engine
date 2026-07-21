# tests/unit/test_health_monitor.py
"""HealthMonitor tests — proxy validation with mocked DB + Redis."""

from unittest.mock import AsyncMock, patch

import pytest

from proxy.health_monitor import HealthMonitor


@pytest.fixture
def pg():
    pg = AsyncMock()
    pg.fetch.return_value = [{"ip": "1.2.3.4", "port": 8080}]
    pg.execute.return_value = "OK"
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
