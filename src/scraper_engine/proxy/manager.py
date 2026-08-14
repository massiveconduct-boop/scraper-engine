# proxy/manager.py
"""Proxy selection from our own scored, persisted pool.

State transitions (per proxy, per domain):
  AVAILABLE → BANNED_FOR_DOMAIN (on failure, TTL 1h) → AVAILABLE (on TTL expiry)

Global reliability_score decays independently of domain-specific bans.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from scraper_engine.config.schema import ProxyTierConfig
from scraper_engine.core.exceptions import ProxyPoolExhaustedError
from scraper_engine.core.models import AnonymityLevel, AsnClass, Proxy, ProxyProtocol
from scraper_engine.proxy.net_probe import lease_preflight
from scraper_engine.proxy.scoring import ScoringEngine, compute_success_rate

if TYPE_CHECKING:
    import asyncpg

    from scraper_engine.core.tenant import TenantId
    from scraper_engine.storage.postgres_client import PostgresClient
    from scraper_engine.storage.redis_client import RedisClient

    from .lease import ProxyLease

logger = logging.getLogger(__name__)

# Debounce window for the harvest-kick signal (round 34) — set by whichever
# exhausted request gets there first via SET NX, so a burst of concurrent
# exhaustions on the same or different domains triggers exactly one
# proxy/harvester_daemon.py out-of-band harvest, not one per request. Kept
# short relative to the daemon's own 60s harvest cooldown (harvester_daemon.py)
# so a kick is never stale by the time the daemon's fast-poll watcher sees it.
HARVEST_KICK_KEY = "proxy:harvest:kick"
HARVEST_KICK_TTL_SECONDS = 30
HARVEST_KICK_CHANNEL = "proxy:events:exhausted"


class ProxyManager:
    """Select a proxy from the persisted, scored pool for a given (level, domain)."""

    MAX_ATTEMPTS: int = 5

    def __init__(
        self,
        redis: RedisClient,
        pg: PostgresClient,
        tier_config: ProxyTierConfig | None = None,
        probe: Callable[[str, int, str], Awaitable[bool]] = lease_preflight,
    ) -> None:
        self._redis = redis
        self._pg = pg
        self._tier_config = tier_config or ProxyTierConfig()
        self._probe = probe

    async def get_proxy(
        self,
        tenant_id: TenantId,
        level: int,
        domain: str,
        sticky: bool = False,
    ) -> ProxyLease:
        """Raises ProxyPoolExhaustedError after MAX_ATTEMPTS bounded retries.

        This closes F-05: no more unbounded recursion. Caller MUST catch
        ProxyPoolExhaustedError and route to escalation/DLQ, never retry blindly.
        """
        from .lease import ProxyLease

        cfg = self._tier_config
        tier_min_score = {
            1: cfg.min_score_level_1,
            2: cfg.min_score_level_2,
            3: cfg.min_score_level_3,
        }.get(level, 50.0)
        # Config-gated stopgap for free-only proxy sources where L3's own
        # ceiling can be structurally unreachable (round 33) — tried once,
        # lazily, only if a real tier-3-caliber proxy search below comes up
        # empty. Never skips searching for a genuine tier-3 proxy first.
        fallback_score = (
            cfg.min_score_level_2 if level == 3 and cfg.allow_tier2_fallback_for_tier3 else None
        )
        fallback_used = False
        seen: set[str] = set()

        for attempt in range(self.MAX_ATTEMPTS):
            proxy = await self._select_candidate(tenant_id, domain, tier_min_score, seen)
            if proxy is None and fallback_score is not None and not fallback_used:
                fallback_used = True
                tier_min_score = fallback_score
                logger.warning(
                    "proxy_tier3_fallback_to_tier2 domain=%s tenant=%s min_score=%.1f",
                    domain,
                    tenant_id,
                    tier_min_score,
                )
                proxy = await self._select_candidate(tenant_id, domain, tier_min_score, seen)
            if proxy is None:
                await self._redis.raw.incr(f"metrics:proxy_exhausted_total:{level}")
                await self._signal_exhaustion()
                raise ProxyPoolExhaustedError(
                    domain=domain,
                    level=level,
                    attempts=attempt + 1,
                )
            seen.add(proxy.key())

            # Check domain-specific ban
            banned = await self._is_banned(tenant_id, proxy, domain)
            if banned:
                continue

            # Fast preflight (round 37, TCP-only; extended same round after
            # a live test caught a proxy that passed a TCP-only check and
            # then dropped mid-navigation) — before round 35's scoring fix,
            # L2/L3 never actually leased a proxy (promotion was
            # structurally unreachable), so a dead lease was never handed
            # to the fetcher. Now that promotion succeeds, a leased proxy
            # is frequently a flaky free one: some are dead on connect
            # (caught by the TCP layer), some accept the connection but
            # never actually forward traffic (caught by the HTTP layer).
            # Without this, either case costs the caller a full 40-60s
            # browser navigation timeout instead of failing in low single
            # digit seconds. Treated exactly like a real fetch failure
            # (mark_failure) so a proxy that keeps failing preflight decays
            # out of the pool the same way one that keeps failing real
            # fetches does.
            if not await self._probe(proxy.ip, proxy.port, proxy.protocol.value):
                await self.mark_failure(tenant_id, proxy.ip, proxy.port, domain)
                continue

            return ProxyLease(proxy=proxy, tenant_id=tenant_id)

        await self._redis.raw.incr(f"metrics:proxy_exhausted_total:{level}")
        await self._signal_exhaustion()
        raise ProxyPoolExhaustedError(domain=domain, level=level, attempts=self.MAX_ATTEMPTS)

    async def _signal_exhaustion(self) -> None:
        """Debounced out-of-band harvest trigger (round 34) — closes the gap
        where the harvester only ran on a fixed timer, fully decoupled from
        real demand. SET NX is the debounce: only the caller that actually
        creates the key (i.e. no kick is already pending) also publishes, so
        a stampede of concurrent exhausted requests produces one signal, not
        one per request. Best-effort — a failure here must never surface as
        a fetch failure, so exceptions are swallowed after a warning."""
        try:
            created = await self._redis.raw.set(
                HARVEST_KICK_KEY, "1", nx=True, ex=HARVEST_KICK_TTL_SECONDS
            )
            if created:
                await self._redis.raw.publish(HARVEST_KICK_CHANNEL, "1")
        except Exception:
            logger.warning("proxy_exhaustion_signal_failed", exc_info=True)

    async def mark_success(self, tenant_id: TenantId, ip: str, port: int) -> None:
        """Improve proxy reliability score on successful fetch.

        Recomputes via ScoringEngine using real accumulated success/failure
        history (round 32 — previously a flat +5 regardless of the proxy's
        real dimensions or track record). Two round trips (increment+read,
        then write); a real-world proxy is never hammered concurrently
        often enough for the small race window this leaves to matter, and
        reliability_score is a heuristic, not something needing strict
        serializability.
        """
        row = await self._pg.fetchrow(
            tenant_id,
            """
            UPDATE proxy_pool
            SET global_success_count = global_success_count + 1,
                last_validated = NOW()
            WHERE ip = $1 AND port = $2
            RETURNING anonymity_level, asn_class, response_time_ms,
                      global_success_count, global_failure_count, last_validated
            """,
            ip,
            port,
        )
        if row is None:
            return
        new_score = self._recompute_score(row)
        await self._pg.execute(
            tenant_id,
            "UPDATE proxy_pool SET reliability_score = $1 WHERE ip = $2 AND port = $3",
            new_score,
            ip,
            port,
        )

    async def mark_failure(self, tenant_id: TenantId, ip: str, port: int, domain: str) -> None:
        """Ban proxy for domain (TTL 1h) and recompute global reliability score.

        See mark_success's docstring for why this is now formula-driven
        instead of a flat -10.
        """
        ban_key = f"proxy_ban:{tenant_id}:{domain}:{ip}:{port}"
        await self._redis.set(tenant_id, ban_key, "1", ttl=3600)

        row = await self._pg.fetchrow(
            tenant_id,
            """
            UPDATE proxy_pool
            SET global_failure_count = global_failure_count + 1
            WHERE ip = $1 AND port = $2
            RETURNING anonymity_level, asn_class, response_time_ms,
                      global_success_count, global_failure_count, last_validated
            """,
            ip,
            port,
        )
        if row is None:
            return
        new_score = self._recompute_score(row)
        await self._pg.execute(
            tenant_id,
            "UPDATE proxy_pool SET reliability_score = $1 WHERE ip = $2 AND port = $3",
            new_score,
            ip,
            port,
        )

    @staticmethod
    def _recompute_score(row: asyncpg.Record) -> float:
        """Shared by mark_success/mark_failure — builds compute_score()'s
        inputs from a proxy_pool row (as returned by the RETURNING clauses
        above, which shape identically to a normal SELECT)."""
        recency_seconds: float | None = None
        last_validated = row["last_validated"]
        if last_validated is not None:
            recency_seconds = (datetime.now(UTC) - last_validated).total_seconds()
        success_rate = compute_success_rate(
            row["global_success_count"],
            row["global_failure_count"],
        )
        result = ScoringEngine().compute_score(
            latency_ms=row["response_time_ms"],
            success_rate=success_rate,
            anonymity=AnonymityLevel(row["anonymity_level"]),
            asn=AsnClass(row["asn_class"]),
            last_validated_seconds_ago=recency_seconds,
        )
        return result.total

    async def _select_candidate(
        self,
        tenant_id: TenantId,
        domain: str,
        min_score: float,
        exclude: set[str],
    ) -> Proxy | None:
        """Select the highest-scored proxy not in the exclude set.

        `exclude` is passed into the query itself (round 37) — not just
        filtered in Python after a fixed LIMIT 20 fetch. Filtering only in
        Python meant every attempt within one get_proxy() call re-fetched
        the SAME top-20-by-score rows; once ~20 of them were excluded
        (domain-banned and/or preflight-failed earlier in the same call),
        _select_candidate returned None and the caller saw
        ProxyPoolExhaustedError even with dozens more viable, lower-ranked
        candidates in the pool. Live-caught (round 37): a domain scraped
        repeatedly in a short window accumulates exactly this — enough of
        its top-20 domain-banned that a fresh get_proxy() call for that
        same domain exhausted in ~1 attempt despite 50+ score-eligible
        proxies existing overall. Excluding in SQL means each attempt's
        LIMIT 20 is a genuinely fresh, not-yet-tried slice.
        """
        rows = await self._pg.fetch(
            tenant_id,
            """
            SELECT id, ip, port, protocol, anonymity_level, asn_class, reliability_score
            FROM proxy_pool
            WHERE reliability_score >= $1
              AND NOT (ip || ':' || port = ANY($2::text[]))
            ORDER BY reliability_score DESC
            LIMIT 20
            """,
            min_score,
            list(exclude),
        )
        for row in rows:
            proxy = Proxy(
                id=row["id"],
                ip=row["ip"],
                port=row["port"],
                protocol=ProxyProtocol(row["protocol"]),
                anonymity_level=row["anonymity_level"],
                asn_class=row["asn_class"],
                reliability_score=row["reliability_score"],
            )
            if proxy.key() not in exclude:
                return proxy
        return None

    async def _is_banned(self, tenant_id: TenantId, proxy: Proxy, domain: str) -> bool:
        """Check if proxy is domain-banned."""
        ban_key = f"proxy_ban:{tenant_id}:{domain}:{proxy.ip}:{proxy.port}"
        return await self._redis.get(tenant_id, ban_key) is not None
