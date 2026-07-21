# proxy/health_monitor.py
"""Periodic re-validation of pooled proxies.

Runs on a configurable interval, re-checks proxies against judge endpoints,
and removes or downgrades proxies that fail validation.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from storage.postgres_client import PostgresClient
    from storage.redis_client import RedisClient

logger = logging.getLogger(__name__)

# Lightweight HTTP endpoints for proxy validation
_JUDGE_URLS = [
    "http://httpbin.org/ip",
    "https://httpbin.org/ip",
]


class HealthMonitor:
    """Periodically re-validate proxies in the pool against judge endpoints.

    Does NOT delete existing pool entries on a single failed cycle
    (avoids flushing a working pool because of a transient judge outage).
    """

    def __init__(self, pg: PostgresClient, redis: RedisClient) -> None:
        self._pg = pg
        self._redis = redis

    async def run_forever(self, interval_seconds: int = 300) -> None:
        """Background loop; never called from a request path."""
        while True:
            try:
                result = await self.check_all()
                logger.info("health_monitor_cycle: %s", result)
            except Exception as exc:
                logger.error("health_monitor_cycle_failed: %s", str(exc))
            await asyncio.sleep(interval_seconds)

    async def check_all(self) -> dict[str, int]:
        """Re-validate all proxies in the pool. Returns {validated, removed, downgraded}."""
        from core.tenant import TenantId

        system_tenant = TenantId("system")
        rows = await self._pg.fetch(
            system_tenant,
            "SELECT ip, port FROM proxy_pool ORDER BY last_validated ASC LIMIT 100",
        )

        validated = 0
        removed = 0
        downgraded = 0

        for row in rows:
            ok = await self.check_one(row["ip"], row["port"])
            if ok:
                await self._pg.execute(
                    system_tenant,
                    "UPDATE proxy_pool SET last_validated = NOW() WHERE ip = $1 AND port = $2",
                    row["ip"],
                    row["port"],
                )
                validated += 1
            else:
                await self._pg.execute(
                    system_tenant,
                    """
                    UPDATE proxy_pool
                    SET reliability_score = GREATEST(0.0, reliability_score - 20.0)
                    WHERE ip = $1 AND port = $2
                    """,
                    row["ip"],
                    row["port"],
                )
                downgraded += 1

            # Remove proxies below minimum reliability
            await self._pg.execute(
                system_tenant,
                "DELETE FROM proxy_pool WHERE reliability_score <= 0",
            )

        return {"validated": validated, "removed": removed, "downgraded": downgraded}

    async def check_one(self, ip: str, port: int) -> bool:
        """Validate a single proxy. Returns True if still working."""
        proxy_url = f"http://{ip}:{port}"
        try:
            async with httpx.AsyncClient(
                proxy=proxy_url, timeout=10.0
            ) as client:
                response = await client.get(_JUDGE_URLS[0])
                ok: bool = response.status_code == 200
                return ok
        except Exception:
            return False
