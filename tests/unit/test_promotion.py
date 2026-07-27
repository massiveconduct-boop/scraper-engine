# tests/unit/test_promotion.py
"""ProxyPromotionJob unit tests — attempt tracking, bounded concurrency, cooldown."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from core.tenant import TenantId
from proxy.promotion import ProxyPromotionJob


def _make_pg_mock(*, fetch_rows=None):
    """Return a PostgresClient mock wired for ProxyPromotionJob use."""
    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=fetch_rows or [])
    conn.execute = AsyncMock()

    class _FakeAcquireCtx:
        async def __aenter__(self):
            return conn
        async def __aexit__(self, *args):
            pass

    pg = MagicMock()
    pg.acquire = MagicMock(return_value=_FakeAcquireCtx())
    return pg


@pytest.fixture
def tenant():
    return TenantId("promo_test")


class TestProxyPromotionJob:
    """Unit tests for proxy promotion with attempt tracking."""

    @pytest.mark.asyncio
    async def test_empty_candidates_returns_zeros(self, tenant):
        """No low-score proxies → all counts zero."""
        pg = _make_pg_mock(fetch_rows=[])
        validate = AsyncMock()
        job = ProxyPromotionJob(pg=pg, http_validate_fn=validate, system_tenant=tenant)

        result = await job.run_once()
        assert result == {"candidates": 0, "promoted": 0, "failed": 0, "exhausted": 0}
        validate.assert_not_called()

    @pytest.mark.asyncio
    async def test_promotes_validating_proxy(self, tenant):
        """Valid HTTP response → score promoted to 60."""
        from core.models import AnonymityLevel

        pg = _make_pg_mock(fetch_rows=[
            {"id": 1, "ip": "10.0.0.1", "port": 3128, "protocol": "HTTP", "promotion_attempts": 0},
        ])
        validate = AsyncMock(return_value=(True, AnonymityLevel.ELITE))
        job = ProxyPromotionJob(pg=pg, http_validate_fn=validate, system_tenant=tenant)

        result = await job.run_once()
        assert result["promoted"] == 1
        assert result["failed"] == 0
        assert result["exhausted"] == 0

    @pytest.mark.asyncio
    async def test_failed_validation_increments_attempts(self, tenant):
        """Failed validation → attempt counter incremented, not promoted."""
        from core.models import AnonymityLevel

        pg = _make_pg_mock(fetch_rows=[
            {"id": 2, "ip": "10.0.0.2", "port": 8080, "protocol": "HTTP", "promotion_attempts": 2},
        ])
        validate = AsyncMock(return_value=(False, AnonymityLevel.TRANSPARENT))
        job = ProxyPromotionJob(pg=pg, http_validate_fn=validate, system_tenant=tenant)

        result = await job.run_once()
        assert result["promoted"] == 0
        assert result["failed"] == 1
        assert result["exhausted"] == 0  # 2+1=3 < 5, not exhausted yet

    @pytest.mark.asyncio
    async def test_proxy_at_max_attempts_is_exhausted(self, tenant):
        """Proxy with 4 attempts, 5th fails → exhausted."""
        from core.models import AnonymityLevel

        pg = _make_pg_mock(fetch_rows=[
            {"id": 3, "ip": "10.0.0.3", "port": 3128, "protocol": "HTTP", "promotion_attempts": 4},
        ])
        validate = AsyncMock(return_value=(False, AnonymityLevel.TRANSPARENT))
        job = ProxyPromotionJob(pg=pg, http_validate_fn=validate, system_tenant=tenant)

        result = await job.run_once()
        assert result["failed"] == 1
        assert result["exhausted"] == 1
        assert result["promoted"] == 0

    @pytest.mark.asyncio
    async def test_query_filters_by_cooldown_and_attempts(self, tenant):
        """Query excludes proxies at max attempts and within cooldown window."""
        from core.models import AnonymityLevel

        conn = AsyncMock()
        conn.fetch = AsyncMock(return_value=[
            {"id": 4, "ip": "10.0.0.4", "port": 3128, "protocol": "HTTP", "promotion_attempts": 0},
        ])
        conn.execute = AsyncMock()

        class _FakeCtx:
            async def __aenter__(self): return conn
            async def __aexit__(self, *a): pass

        pg = MagicMock()
        pg.acquire = MagicMock(return_value=_FakeCtx())

        validate = AsyncMock(return_value=(True, AnonymityLevel.ELITE))
        job = ProxyPromotionJob(pg=pg, http_validate_fn=validate, system_tenant=tenant)

        await job.run_once()

        # Verify the query filters by promotion_attempts and cooldown
        fetch_call = conn.fetch.call_args
        query = fetch_call[0][0]
        assert "promotion_attempts < " in query
        assert "last_promotion_attempt_at" in query
        assert "INTERVAL" in query
        assert "LIMIT" in query

    @pytest.mark.asyncio
    async def test_semaphore_bounds_concurrency(self, tenant):
        """Verify semaphore is created with PROMOTION_CONCURRENCY=5."""
        from core.models import AnonymityLevel
        validate = AsyncMock(return_value=(False, AnonymityLevel.TRANSPARENT))
        job = ProxyPromotionJob(pg=_make_pg_mock(), http_validate_fn=validate, system_tenant=tenant)

        # Semaphore should exist and have value of 5 (plan §4.2: PROMOTION_CONCURRENCY=5)
        assert job._sem is not None
        assert job._sem._value == 5, (
            f"Expected PROMOTION_CONCURRENCY=5, got {job._sem._value}"
        )
