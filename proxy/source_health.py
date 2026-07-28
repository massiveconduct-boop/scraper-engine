# proxy/source_health.py
"""Per-source proxy health gauge.

Runs alongside the harvester loop. Tracks per-source success/failure over time
and exports a gauge per source, so a source going dark shows up as a specific,
named signal — not just a drop in the aggregate pool count that could have any
cause (round 13 D2).

The 5-6 source count was accepted as a ceiling in round 6, but "accepted" needs
an ongoing watcher, not a one-time decision — these free sources go dark on
their own schedule, independent of this project's release cycle. The
ProxySourceWentDark alert (monitoring/alerts/prometheus_rules.yml) fires on
sustained absence.

Round 25: this used to only set an in-process Gauge. `record_source_health`
is called from `ProxyHarvester._direct_scrape`, which runs inside the
proxy-harvester container — a different process from the one serving
/metrics (api). An in-process Gauge set there could never reach Prometheus;
same cross-process gap already closed for the other round-25 metrics (see
observability/metrics.py's module docstring). Fixed the same way: write to
Redis at event time, refresh the Gauge from Redis only when /metrics is
actually scraped, from the api process.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from prometheus_client import REGISTRY, Gauge

if TYPE_CHECKING:
    from storage.redis_client import RedisClient

proxy_source_healthy = Gauge(
    "proxy_source_healthy",
    "1 if the named proxy source returned >=1 proxy in the last harvest cycle, else 0",
    ["source_name"],
    registry=REGISTRY,
)

REDIS_KEY_PREFIX = "metrics:proxy_source_healthy:"


async def record_source_health(redis: RedisClient, source_name: str, proxy_count: int) -> None:
    """Write the per-source health signal to Redis from a single harvest
    cycle's result — no extra work, no extra query, just a different sink
    than the in-process Gauge (which nothing scrapes from this process)."""
    await redis.raw.set(f"{REDIS_KEY_PREFIX}{source_name}", "1" if proxy_count > 0 else "0")
