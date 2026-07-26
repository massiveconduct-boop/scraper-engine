"""Application-side Prometheus gauge for validated proxy count.

Single source of truth for validated proxy count: count_validated_proxies().
Called by both /metrics endpoint (api/routes.py) and harvester daemon
(proxy/harvester.py). No duplicate query — one function, one SQL string.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from prometheus_client import REGISTRY, Gauge

if TYPE_CHECKING:
    from core.tenant import TenantId
    from storage.postgres_client import PostgresClient

proxy_pool_validated_count = Gauge(
    "proxy_pool_validated_count",
    "Number of proxies with reliability_score >= 40 (L1 threshold)",
    registry=REGISTRY,
)


async def count_validated_proxies(pg: PostgresClient, tenant: TenantId) -> int:
    """Return count of proxies with reliability_score >= 40.

    Single source of truth — used by both the /metrics endpoint and the
    harvester daemon. No other file contains this query.
    """
    rows = await pg.fetch(
        tenant,
        "SELECT COUNT(*) as n FROM proxy_pool WHERE reliability_score >= 40",
    )
    return rows[0]["n"] if rows else 0
