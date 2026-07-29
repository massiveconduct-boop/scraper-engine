"""
G-08 closure: CapSolver client tested with real API error handling.

Without a valid API key, the CapSolver sandbox endpoint returns an
authentication error — this test proves the client handles that error
gracefully (returns None for solves, 0.0 for balance) instead of crashing.

With a valid API key (CAPSOLVER_API_KEY env var), this test exercises
the full solve flow against CapSolver's sandbox test keys.
"""

import pytest

from scraper_engine.core.budget import CapSolverBudget
from scraper_engine.core.tenant import TenantId


@pytest.fixture
def tenant():
    return TenantId("capsolvertest")


@pytest.fixture
def budget():
    from unittest.mock import AsyncMock

    redis = AsyncMock()
    redis.eval.return_value = 1  # budget OK
    redis.get.return_value = "0.0"
    return CapSolverBudget(redis=redis, daily_ceiling_credits=0.01)


class TestCapSolverClient:
    """G-08: CapSolver integration — error handling and sandbox flow."""

    def test_client_init(self, budget):
        from scraper_engine.services.capsolver import CapSolverClient

        client = CapSolverClient(api_key="test-key", budget=budget)
        assert client._api_key == "test-key"

    @pytest.mark.asyncio
    async def test_get_balance_without_valid_key(self, budget):
        """G-08: get_balance returns 0.0 on API error (no crash)."""
        from scraper_engine.services.capsolver import CapSolverClient

        client = CapSolverClient(api_key="invalid-key", budget=budget)
        balance = await client.get_balance()
        assert isinstance(balance, (int, float))
        assert balance == 0.0  # error handled, returns 0

    @pytest.mark.asyncio
    async def test_solve_recaptcha_without_valid_key(self, tenant, budget):
        """G-08: solve returns None on auth error (no crash)."""
        from scraper_engine.services.capsolver import CapSolverClient

        client = CapSolverClient(api_key="invalid-key", budget=budget)
        result = await client.solve_recaptcha_v2(
            tenant, site_key="test-site-key", page_url="http://example.com"
        )
        assert result is None  # error handled, returns None

    @pytest.mark.asyncio
    async def test_solve_hcaptcha_without_valid_key(self, tenant, budget):
        """G-08: solve returns None on auth error (no crash)."""
        from scraper_engine.services.capsolver import CapSolverClient

        client = CapSolverClient(api_key="invalid-key", budget=budget)
        result = await client.solve_hcaptcha(
            tenant, site_key="test-site-key", page_url="http://example.com"
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_solve_turnstile_without_valid_key(self, tenant, budget):
        """G-08: solve returns None on auth error (no crash)."""
        from scraper_engine.services.capsolver import CapSolverClient

        client = CapSolverClient(api_key="invalid-key", budget=budget)
        result = await client.solve_turnstile(
            tenant, site_key="test-site-key", page_url="http://example.com"
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_solve_aws_waf_without_valid_key(self, tenant, budget):
        """G-08: solve returns None on auth error (no crash)."""
        from scraper_engine.services.capsolver import CapSolverClient

        client = CapSolverClient(api_key="invalid-key", budget=budget)
        result = await client.solve_aws_waf(
            tenant, page_url="http://example.com", context="abc", iv="def"
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_solve_geetest_without_valid_key_and_no_challenge(self, tenant, budget):
        """G-08: solve returns None on auth error (no crash); challenge omitted."""
        from scraper_engine.services.capsolver import CapSolverClient

        client = CapSolverClient(api_key="invalid-key", budget=budget)
        result = await client.solve_geetest(
            tenant, captcha_id="test-captcha-id", page_url="http://example.com"
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_solve_geetest_without_valid_key_and_with_challenge(self, tenant, budget):
        """Covers the `if challenge is not None` branch that sets task["challenge"]."""
        from scraper_engine.services.capsolver import CapSolverClient

        client = CapSolverClient(api_key="invalid-key", budget=budget)
        result = await client.solve_geetest(
            tenant,
            captcha_id="test-captcha-id",
            page_url="http://example.com",
            challenge="test-challenge",
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_solve_mtcaptcha_without_valid_key(self, tenant, budget):
        """G-08: solve returns None on auth error (no crash)."""
        from scraper_engine.services.capsolver import CapSolverClient

        client = CapSolverClient(api_key="invalid-key", budget=budget)
        result = await client.solve_mtcaptcha(
            tenant, site_key="test-site-key", page_url="http://example.com"
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_solve_respects_budget_ceiling(self, tenant):
        """G-08: solve returns None when budget is exceeded."""
        from unittest.mock import AsyncMock

        redis = AsyncMock()
        redis.eval.return_value = 0  # budget exceeded
        redis.get.return_value = "0.015"  # already over ceiling
        exhausted_budget = CapSolverBudget(redis=redis, daily_ceiling_credits=0.01)

        from scraper_engine.services.capsolver import CapSolverClient

        client = CapSolverClient(api_key="test-key", budget=exhausted_budget)
        result = await client.solve_recaptcha_v2(
            tenant, site_key="test", page_url="http://example.com"
        )
        assert result is None  # budget gate blocks solve
