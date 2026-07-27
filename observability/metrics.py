"""Application-side Prometheus metrics.

Single source of truth for validated proxy count: count_validated_proxies().
Called by both /metrics endpoint (api/routes.py) and harvester daemon
(proxy/harvester.py). No duplicate query — one function, one SQL string.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from prometheus_client import REGISTRY, Counter, Gauge

if TYPE_CHECKING:
    from core.tenant import TenantId
    from storage.postgres_client import PostgresClient

proxy_pool_validated_count = Gauge(
    "proxy_pool_validated_count",
    "Number of proxies with reliability_score >= 40 (L1 threshold)",
    registry=REGISTRY,
)

safe_content_none_total = Counter(
    "safe_content_none_total",
    "Number of times Level3Fetcher._safe_content returned None "
    "(page.content() raised mid-navigation — guard fired, loop kept polling)",
    registry=REGISTRY,
)

captcha_solve_attempts_total = Counter(
    "captcha_solve_attempts_total",
    "In-page CAPTCHA solve attempts — a solvable widget was detected on a "
    "challenge page and a token solve was requested from the provider.",
    ["kind"],
    registry=REGISTRY,
)

captcha_solved_total = Counter(
    "captcha_solved_total",
    "In-page CAPTCHA solves that produced a token and injected it into the "
    "page (does not assert the site accepted it — only that injection ran).",
    ["kind"],
    registry=REGISTRY,
)

captcha_provider_configured = Gauge(
    "captcha_provider_configured",
    "1 if a CAPTCHA provider API key is present in the environment, else 0. "
    "Configured != verified: a key can be present but rejected (inactive "
    "capability / 401). Run tools/validate_captcha_keys.py to confirm the key "
    "is actually accepted by the provider.",
    ["provider"],
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
