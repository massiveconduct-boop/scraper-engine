# tests/unit/test_retention_reaper.py
"""RetentionReaper tests — mocked PostgresClient, no real DB.

Closes the gap where SessionRetentionConfig's ttl/retention-day fields were
declared but nothing ever enforced them (browser_sessions/domain_ban_history
grew unbounded).
"""

from unittest.mock import AsyncMock

import pytest

from scraper_engine.config.schema import SessionRetentionConfig
from scraper_engine.proxy.retention_reaper import RetentionReaper


@pytest.fixture
def cfg():
    return SessionRetentionConfig(
        browser_sessions_ttl_days=30,
        domain_ban_history_retention_days=7,
        cleanup_interval_seconds=3600,
    )


@pytest.fixture
def pg():
    return AsyncMock()


class TestRetentionReaper:
    def test_init(self, pg, cfg):
        reaper = RetentionReaper(pg, cfg)
        assert reaper._pg is pg
        assert reaper._cfg is cfg

    @pytest.mark.asyncio
    async def test_run_once_sums_deletions_across_tenants(self, pg, cfg):
        pg.fetch.return_value = [{"tenant_id": "acme"}, {"tenant_id": "other"}]
        pg.execute.side_effect = ["DELETE 2", "DELETE 3", "DELETE 5"]

        reaper = RetentionReaper(pg, cfg)
        result = await reaper.run_once()

        assert result == {
            "browser_sessions_deleted": 5,
            "domain_ban_history_deleted": 5,
            "tenants_failed": 0,
        }

    @pytest.mark.asyncio
    async def test_run_once_zero_when_nothing_expired(self, pg, cfg):
        pg.fetch.return_value = [{"tenant_id": "acme"}]
        pg.execute.return_value = "DELETE 0"

        reaper = RetentionReaper(pg, cfg)
        result = await reaper.run_once()

        assert result == {
            "browser_sessions_deleted": 0,
            "domain_ban_history_deleted": 0,
            "tenants_failed": 0,
        }

    @pytest.mark.asyncio
    async def test_run_once_no_tenants_still_reaps_ban_history(self, pg, cfg):
        pg.fetch.return_value = []
        pg.execute.return_value = "DELETE 1"

        reaper = RetentionReaper(pg, cfg)
        result = await reaper.run_once()

        assert result["browser_sessions_deleted"] == 0
        assert result["domain_ban_history_deleted"] == 1
        pg.execute.assert_awaited_once()  # only the ban-history delete ran

    @pytest.mark.asyncio
    async def test_one_tenant_failure_does_not_block_others_or_ban_history(self, pg, cfg):
        """A stale tenant schema (e.g. missing a column a later migration added)
        must not prevent reaping other tenants or domain_ban_history — found
        live: one out-of-date dev tenant schema silently killed the entire
        cycle before this isolation was added."""
        pg.fetch.return_value = [
            {"tenant_id": "stale_tenant"},
            {"tenant_id": "healthy_tenant"},
        ]
        pg.execute.side_effect = [
            Exception('column "expires_at" does not exist'),
            "DELETE 4",
            "DELETE 1",
        ]

        reaper = RetentionReaper(pg, cfg)
        result = await reaper.run_once()

        assert result == {
            "browser_sessions_deleted": 4,
            "domain_ban_history_deleted": 1,
            "tenants_failed": 1,
        }

    @pytest.mark.asyncio
    async def test_domain_ban_history_query_uses_configured_retention_days(self, pg, cfg):
        pg.fetch.return_value = []
        pg.execute.return_value = "DELETE 0"

        reaper = RetentionReaper(pg, cfg)
        await reaper.run_once()

        args, _kwargs = pg.execute.await_args
        assert "domain_ban_history" in args[1]
        assert args[2] == cfg.domain_ban_history_retention_days
