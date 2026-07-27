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
"""

from __future__ import annotations

from prometheus_client import REGISTRY, Gauge

proxy_source_healthy = Gauge(
    "proxy_source_healthy",
    "1 if the named proxy source returned >=1 proxy in the last harvest cycle, else 0",
    ["source_name"],
    registry=REGISTRY,
)


def record_source_health(source_name: str, proxy_count: int) -> None:
    """Set the per-source health gauge from a single harvest cycle's result.

    Called once per source inside ProxyHarvester._direct_scrape, using the
    per-source count already being computed there — no extra work, no extra
    query.
    """
    proxy_source_healthy.labels(source_name=source_name).set(1 if proxy_count > 0 else 0)
