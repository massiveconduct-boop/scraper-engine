# proxy/health_monitor.py
"""Periodic re-validation of pooled proxies.

Runs on a configurable interval, re-checks proxies against judge endpoints,
and removes, downgrades, or rescores proxies based on the result.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from scraper_engine.core.models import AnonymityLevel, AsnClass
from scraper_engine.proxy.harvester import ProxyHarvester, SupportsClassify, _to_asn_class
from scraper_engine.proxy.scoring import ScoringEngine, compute_success_rate

if TYPE_CHECKING:
    import asyncpg

    from scraper_engine.storage.postgres_client import PostgresClient
    from scraper_engine.storage.redis_client import RedisClient

logger = logging.getLogger(__name__)

# Bounded parallel judge validations — matches promotion.py's
# PROMOTION_CONCURRENCY. Needed once check_all started doing real per-proxy
# work (judge validation + a DNS classify() call, round 38): sequentially
# awaiting up to 100 rows each taking up to ~5-7s worst case ballooned a
# single cycle to 15-25+ minutes, well past the configured 300s interval —
# confirmed live (no cycle completed in 10+ minutes post-deploy while rows
# were visibly still being processed one at a time). Bounding to 5
# concurrent checks brings a full 100-row cycle back down to roughly
# 1/5th the sequential wall-clock time.
HEALTH_CHECK_CONCURRENCY = 5


def _deleted_row_count(status: str) -> int:
    """asyncpg's execute() returns a command tag like "DELETE 3" — parse the
    row count so `removed` reflects reality (it was always 0 before: the
    DELETE ran every cycle but its result was discarded, not counted)."""
    parts = status.split()
    return int(parts[-1]) if parts and parts[-1].isdigit() else 0


class HealthMonitor:
    """Periodically re-validate proxies in the pool against judge endpoints.

    Does NOT delete existing pool entries on a single failed cycle
    (avoids flushing a working pool because of a transient judge outage).
    """

    def __init__(
        self,
        pg: PostgresClient,
        redis: RedisClient,
        asn_classifier: SupportsClassify | None = None,
    ) -> None:
        from scraper_engine.proxy.asn_classifier import NullAsnClassifier

        self._pg = pg
        self._redis = redis
        self._classifier: SupportsClassify = asn_classifier or NullAsnClassifier()

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
        """Re-validate all proxies in the pool. Returns {validated, removed, downgraded}.

        Round 38 — a passing validation now rescores the proxy from a fresh,
        accurately-measured reading (anonymity/asn/latency, via the same
        `_http_validate` this cycle already calls, folded together with the
        proxy's real accumulated success/failure history) instead of only
        bumping `last_validated`. Previously `response_time_ms` (and
        anonymity_level/asn_class) were captured exactly once, at
        harvest/promotion time, and never touched again for the rest of a
        proxy's life — this cycle already re-checks every proxy on a
        rolling basis (oldest-`last_validated`-first, so it eventually
        covers the whole pool) but was discarding everything except a
        boolean pass/fail. Root cause of L3 (score >=90) staying stuck at 0
        even as the pool grew: a proxy's score was permanently capped by
        whatever single latency sample it happened to get once, which
        (before this same round's `_http_validate` fix) was frequently
        inflated by dead-judge timeout time — now that reading gets a
        genuine chance to improve every cycle instead of being frozen.
        """
        from scraper_engine.core.tenant import TenantId

        system_tenant = TenantId("system")
        rows = await self._pg.fetch(
            system_tenant,
            """SELECT ip, port, protocol, global_success_count, global_failure_count
               FROM proxy_pool ORDER BY last_validated ASC LIMIT 100""",
        )

        sem = asyncio.Semaphore(HEALTH_CHECK_CONCURRENCY)

        async def _check(
            row: asyncpg.Record,
        ) -> tuple[asyncpg.Record, tuple[bool, AnonymityLevel, AsnClass, int | None]]:
            async with sem:
                result = await self.check_one(row["ip"], row["port"], row["protocol"])
            return row, result

        checked = await asyncio.gather(*[_check(row) for row in rows]) if rows else []

        validated = 0
        downgraded = 0

        for row, (is_valid, anonymity, asn, latency_ms) in checked:
            ip, port = row["ip"], row["port"]
            if is_valid:
                success_rate = compute_success_rate(
                    row["global_success_count"],
                    row["global_failure_count"],
                )
                score = ScoringEngine().compute_score(
                    latency_ms=latency_ms,
                    success_rate=success_rate,
                    anonymity=anonymity,
                    asn=asn,
                    last_validated_seconds_ago=0,
                ).total
                await self._pg.execute(
                    system_tenant,
                    """
                    UPDATE proxy_pool
                    SET anonymity_level = $1, asn_class = $2, response_time_ms = $3,
                        reliability_score = $4, last_validated = NOW()
                    WHERE ip = $5 AND port = $6
                    """,
                    anonymity.value,
                    asn.value,
                    latency_ms,
                    score,
                    ip,
                    port,
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
                    ip,
                    port,
                )
                downgraded += 1

        # One pass at the end, not once per row — the old per-row DELETE was
        # redundant (each iteration deleted every currently-zero-score row
        # regardless of which row triggered it) and, more importantly, ran
        # sequentially inside what's now a concurrently-checked loop.
        deleted = await self._pg.execute(
            system_tenant,
            "DELETE FROM proxy_pool WHERE reliability_score <= 0",
        )
        removed = _deleted_row_count(deleted)

        # api/health.py reads this key for GET /health's proxy_pool_size —
        # it was being read but never written anywhere, so the field was
        # permanently stuck at 0 regardless of the pool's real state.
        pool_row = await self._pg.fetchrow(system_tenant, "SELECT count(*) AS n FROM proxy_pool")
        pool_size = pool_row["n"] if pool_row else 0
        await self._redis.set(system_tenant, "metrics:proxy_pool_size", str(pool_size), ttl=600)

        return {"validated": validated, "removed": removed, "downgraded": downgraded}

    async def check_one(
        self, ip: str, port: int, protocol: str = "HTTP"
    ) -> tuple[bool, AnonymityLevel, AsnClass, int | None]:
        """Validate a single proxy. Returns (is_valid, anonymity, asn, latency_ms).

        Delegates to `ProxyHarvester._http_validate` (round 38 — was a
        hand-rolled duplicate of the same JUDGE_URLS loop that only ever
        returned a bare bool, discarding the anonymity/latency data
        `_http_validate` already computes) so this module's validation and
        the harvester's stay a single implementation, including the
        accurate winning-request-only latency measurement.
        """
        is_valid, anonymity, latency_ms = await ProxyHarvester._http_validate(ip, port, protocol)
        if not is_valid:
            return False, AnonymityLevel.TRANSPARENT, AsnClass.UNKNOWN, None
        asn = _to_asn_class(await self._classifier.classify(ip))
        return True, anonymity, asn, latency_ms
