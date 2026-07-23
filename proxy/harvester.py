# proxy/harvester.py
"""Background proxy discovery — owns all proxybroker2 Python API calls.

Lifecycle: runs as its own supervisord/systemd-managed process, independent of
API/worker processes. A crash here degrades proxy freshness, not request-path availability.

State/Concurrency: single-writer to proxy_pool (upserts on (ip, port, protocol)).
Safe to run exactly one replica. 2+ replicas waste judge-server quota without benefit.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Protocol

from core.models import AnonymityLevel, AsnClass, ProxyProtocol

if TYPE_CHECKING:
    from storage.postgres_client import PostgresClient

logger = logging.getLogger(__name__)


class AsnClassifier(Protocol):
    """Protocol for ASN classification — swap MaxMind GeoLite2-ASN for paid IP-reputation API."""

    async def classify(self, ip: str) -> str:
        ...


class ProxyHarvester:
    """Background loop that discovers, validates, and persists free proxies."""

    def __init__(
        self, pg: PostgresClient, sources: list[str], asn_classifier: AsnClassifier
    ) -> None:
        self._pg = pg
        self._sources = sources
        self._classifier = asn_classifier

    async def run_forever(self, interval_seconds: int = 600) -> None:
        """Background loop; never called from a request path."""
        while True:
            try:
                count = await self.harvest_once()
                logger.info("harvest_cycle_complete: %d discovered", count)
            except Exception as exc:
                logger.error("harvest_cycle_failed: %s", str(exc))
                await asyncio.sleep(min(interval_seconds, 600))
            await asyncio.sleep(interval_seconds)

    async def harvest_once(self, limit: int = 200) -> int:
        """Use proxybroker's Broker.find() async generator directly (in-process).

        NOT proxybroker2's serve daemon (which is a proxy-rotation gateway, not a control API).

        Returns count of newly validated proxies written.
        """
        from core.tenant import TenantId

        count = 0
        system_tenant = TenantId("system")

        try:
            from proxybroker2 import Broker
        except ImportError:
            logger.warning("proxybroker2 not installed — proxy harvesting disabled")
            return 0

        broker = Broker(sources=self._sources)
        try:
            proxy_stream = await broker.find(limit=limit, types=["HTTP", "HTTPS"])
        except Exception as exc:
            logger.warning("proxy source fetch failed: %s", str(exc))
            return 0
        if proxy_stream is None:
            logger.warning("proxybroker2 returned no results — trying direct scrape fallback")
            count = await self._direct_scrape(limit, system_tenant)
            return count
        async for proxy_data in proxy_stream:
            try:
                asn = await self._classifier.classify(proxy_data.get("ip", "0.0.0.0"))
                asn_class = AsnClass.RESIDENTIAL if "residential" in asn.lower() else (
                    AsnClass.DATACENTER if "datacenter" in asn.lower() else AsnClass.UNKNOWN
                )

                anonymity_map = {
                    "elite": AnonymityLevel.ELITE,
                    "anonymous": AnonymityLevel.ANONYMOUS,
                    "transparent": AnonymityLevel.TRANSPARENT,
                }
                anonymity = anonymity_map.get(
                    proxy_data.get("anonymity", "transparent"), AnonymityLevel.TRANSPARENT
                )
                protocol_str = proxy_data.get("protocol", "HTTP").upper()
                valid_protocols = {"HTTP", "HTTPS", "SOCKS4", "SOCKS5"}
                protocol = (
                    ProxyProtocol(protocol_str)
                    if protocol_str in valid_protocols
                    else ProxyProtocol.HTTP
                )

                await self._pg.execute(
                    system_tenant,
                    """
                    INSERT INTO proxy_pool (ip, port, protocol, anonymity_level,
                                            asn_class, reliability_score, last_validated)
                    VALUES ($1, $2, $3, $4, $5, 50.0, NOW())
                    ON CONFLICT (ip, port, protocol) DO UPDATE
                    SET last_validated = NOW()
                    """,
                    proxy_data.get("ip"),
                    proxy_data.get("port", 0),
                    protocol.value,
                    anonymity.value,
                    asn_class.value,
                )
                count += 1
            except Exception:
                continue

        return count

    async def _direct_scrape(self, limit: int, tenant: "TenantId") -> int:
        """Fallback: scrape proxy APIs directly when proxybroker2 returns none.

        BD-01: proxybroker2's provider modules have outdated parsing —
        upstream APIs return real proxy data but the library cannot parse them.
        This method bypasses proxybroker2 and parses the raw API responses.
        """
        import re

        import httpx

        sources = [
            (
                "https://api.proxyscrape.com/v2/"
                "?request=displayproxies&protocol=http&timeout=10000"
                "&country=all&ssl=all&anonymity=all",
                "ip_port",
            ),
        ]

        count = 0
        async with httpx.AsyncClient(timeout=15) as client:
            for url, fmt in sources:
                try:
                    resp = await client.get(url)
                    resp.raise_for_status()
                    text = resp.text.strip()
                    if not text:
                        continue

                    lines = text.split("\n")[:limit]
                    for line in lines:
                        line = line.strip()
                        if not line or ":" not in line:
                            continue
                        if fmt == "ip_port":
                            ip, port_str = line.rsplit(":", 1)
                            try:
                                port = int(port_str)
                            except ValueError:
                                continue
                            if not re.match(r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$", ip):
                                continue

                            try:
                                await self._pg.execute(
                                    tenant,
                                    """INSERT INTO proxy_pool
                                       (ip, port, protocol, anonymity, asn_class)
                                       VALUES ($1, $2, $3, $4, $5)
                                       ON CONFLICT (ip, port) DO NOTHING""",
                                    ip, port, "HTTP", "anonymous", "unknown",
                                )
                                count += 1
                                if count >= limit:
                                    return count
                            except Exception:
                                continue

                except Exception as exc:
                    logger.warning("direct proxy scrape failed for %s: %s", url, str(exc))
                    continue

        return count
