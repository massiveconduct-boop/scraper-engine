# proxy/manager.py
"""Proxy selection from our own scored, persisted pool.

State transitions (per proxy, per domain):
  AVAILABLE → BANNED_FOR_DOMAIN (on failure, TTL 1h) → AVAILABLE (on TTL expiry)

Global reliability_score decays independently of domain-specific bans.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from core.exceptions import ProxyPoolExhaustedError
from core.models import Proxy, ProxyProtocol

if TYPE_CHECKING:
    from core.tenant import TenantId
    from storage.postgres_client import PostgresClient
    from storage.redis_client import RedisClient

    from .lease import ProxyLease


class ProxyManager:
    """Select a proxy from the persisted, scored pool for a given (level, domain)."""

    MAX_ATTEMPTS: int = 5

    def __init__(self, redis: RedisClient, pg: PostgresClient) -> None:
        self._redis = redis
        self._pg = pg

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

        tier_min_score = {1: 40.0, 2: 70.0, 3: 90.0}.get(level, 50.0)
        seen: set[str] = set()

        for attempt in range(self.MAX_ATTEMPTS):
            proxy = await self._select_candidate(tenant_id, domain, tier_min_score, seen)
            if proxy is None:
                await self._redis.raw.incr(f"metrics:proxy_exhausted_total:{level}")
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

            return ProxyLease(proxy=proxy, tenant_id=tenant_id)

        await self._redis.raw.incr(f"metrics:proxy_exhausted_total:{level}")
        raise ProxyPoolExhaustedError(domain=domain, level=level, attempts=self.MAX_ATTEMPTS)

    async def mark_success(self, tenant_id: TenantId, ip: str, port: int) -> None:
        """Improve proxy reliability score on successful fetch."""
        await self._pg.execute(
            tenant_id,
            """
            UPDATE proxy_pool SET reliability_score = LEAST(100.0, reliability_score + 5.0),
                                 last_validated = NOW()
            WHERE ip = $1 AND port = $2
            """,
            ip,
            port,
        )

    async def mark_failure(
        self, tenant_id: TenantId, ip: str, port: int, domain: str
    ) -> None:
        """Ban proxy for domain (TTL 1h) and decay global reliability score."""
        ban_key = f"proxy_ban:{tenant_id}:{domain}:{ip}:{port}"
        await self._redis.set(tenant_id, ban_key, "1", ttl=3600)

        await self._pg.execute(
            tenant_id,
            """
            UPDATE proxy_pool SET reliability_score = GREATEST(0.0, reliability_score - 10.0)
            WHERE ip = $1 AND port = $2
            """,
            ip,
            port,
        )

    async def _select_candidate(
        self,
        tenant_id: TenantId,
        domain: str,
        min_score: float,
        exclude: set[str],
    ) -> Proxy | None:
        """Select the highest-scored proxy not in the exclude set."""
        rows = await self._pg.fetch(
            tenant_id,
            """
            SELECT id, ip, port, protocol, anonymity_level, asn_class, reliability_score
            FROM proxy_pool
            WHERE reliability_score >= $1
            ORDER BY reliability_score DESC
            LIMIT 20
            """,
            min_score,
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

    async def _is_banned(
        self, tenant_id: TenantId, proxy: Proxy, domain: str
    ) -> bool:
        """Check if proxy is domain-banned."""
        ban_key = f"proxy_ban:{tenant_id}:{domain}:{proxy.ip}:{proxy.port}"
        return await self._redis.get(tenant_id, ban_key) is not None
