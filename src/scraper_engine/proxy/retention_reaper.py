# proxy/retention_reaper.py
"""Enforces SessionRetentionConfig — without this, browser_sessions and
domain_ban_history grow unbounded forever (both tables' retention config
fields existed but nothing ever read them).

browser_sessions.expires_at already carries the TTL as an absolute timestamp
(defaulted at insert time — migrations/versions/002_browser_sessions_schema.py),
so this reaper only deletes rows already past it; browser_sessions_ttl_days
stays the documented default for that column, not a second source of truth.
domain_ban_history has no such column, so domain_ban_history_retention_days
is applied here directly against banned_until.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from scraper_engine.core.tenant import TenantId
from scraper_engine.proxy.health_monitor import _deleted_row_count

if TYPE_CHECKING:
    from scraper_engine.config.schema import SessionRetentionConfig
    from scraper_engine.storage.postgres_client import PostgresClient

logger = logging.getLogger(__name__)

_SYSTEM_TENANT = TenantId("system")


class RetentionReaper:
    """One-shot-callable retention sweep — run on a timer by harvester_daemon,
    or manually via `cli reap`."""

    def __init__(self, pg: PostgresClient, cfg: SessionRetentionConfig) -> None:
        self._pg = pg
        self._cfg = cfg

    async def run_once(self) -> dict[str, int]:
        """Delete expired browser_sessions (every tenant schema) and stale
        domain_ban_history rows (public schema). Returns counts deleted.

        One tenant's schema being out of date with the current
        create_tenant_schema() shape (e.g. a tenant created before a later
        migration reshaped browser_sessions) must not block reaping for every
        other tenant or for domain_ban_history — each tenant is isolated the
        same way harvester_daemon.py isolates harvest/promotion/health/
        retention from each other.
        """
        sessions_deleted = 0
        tenants_failed = 0
        rows = await self._pg.fetch(_SYSTEM_TENANT, "SELECT tenant_id FROM public.tenants")
        for row in rows:
            tenant = TenantId(row["tenant_id"])
            try:
                status = await self._pg.execute(
                    tenant, "DELETE FROM browser_sessions WHERE expires_at < NOW()"
                )
                sessions_deleted += _deleted_row_count(status)
            except Exception:
                tenants_failed += 1
                logger.exception("retention_reap_tenant_failed tenant=%s", tenant)

        ban_status = await self._pg.execute(
            _SYSTEM_TENANT,
            "DELETE FROM public.domain_ban_history "
            "WHERE banned_until < NOW() - make_interval(days => $1)",
            self._cfg.domain_ban_history_retention_days,
        )
        ban_history_deleted = _deleted_row_count(ban_status)

        result = {
            "browser_sessions_deleted": sessions_deleted,
            "domain_ban_history_deleted": ban_history_deleted,
            "tenants_failed": tenants_failed,
        }
        logger.info("retention_reap_cycle: %s", result)
        return result
