# proxy/harvester.py
"""Proxy harvester — discovers, validates, and persists free proxies.

Uses proxybroker2 for provider-based proxy gathering with a queue-drain
pattern. Falls back to direct API scraping when proxybroker2 returns
no results (BD-01: some upstream APIs changed their response format).
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import TYPE_CHECKING

import httpx

from core.models import AnonymityLevel, AsnClass, ProxyProtocol

if TYPE_CHECKING:
    from core.tenant import TenantId
    from storage.postgres_client import PostgresClient

logger = logging.getLogger(__name__)


class FakeClassifier:
    """Default ASN classifier — returns 'unknown' when no classifier configured."""

    async def classify(self, ip: str) -> str:
        return "unknown"


class ProxyHarvester:
    """Discovers proxies from free sources and persists them."""

    def __init__(
        self,
        pg: PostgresClient,
        sources: list[str] | None = None,
        asn_classifier: object | None = None,
    ) -> None:
        self._pg = pg
        self._sources = sources or []
        self._classifier = asn_classifier or FakeClassifier()

    async def harvest_once(self, limit: int = 100) -> int:
        """Run one harvest cycle.

        Returns count of newly validated proxies written.
        """
        from core.tenant import TenantId

        system_tenant = TenantId("system")

        # Primary: direct API scraping (proxybroker2's 38 providers use
        # web-scraping with outdated HTML parsers — all currently broken).
        count = await self._direct_scrape(limit, system_tenant)
        if count > 0:
            return count

        # Fallback: proxybroker2 (when providers are updated upstream)
        try:
            count = await self._harvest_via_broker(limit, system_tenant)
        except Exception as exc:
            logger.warning("proxybroker2 harvest failed: %s", exc)

        return count

    async def _harvest_via_broker(self, limit: int, tenant: TenantId) -> int:
        """Use proxybroker2 with its 38 default providers + any custom sources.

        proxybroker2 uses a push pattern: pass asyncio.Queue() to Broker(),
        call broker.find() to start background collection, drain queue
        concurrently via asyncio.gather(). broker sends None sentinel when done.
        """
        try:
            from proxybroker2 import Broker
        except ImportError:
            logger.warning("proxybroker2 not installed")
            return 0

        queue = asyncio.Queue()
        broker = Broker(
            queue,
            providers=(self._sources if self._sources else [
                "https://api.proxyscrape.com/?request=getproxies&proxytype=http",
            ]),
            timeout=15,
            max_conn=10,
            max_tries=1,
            verify_ssl=False,
        )

        grabbed: list = []

        async def _drain() -> None:
            while len(grabbed) < limit:
                try:
                    proxy = await asyncio.wait_for(queue.get(), timeout=60)
                    if proxy is None:
                        break
                    grabbed.append(proxy)
                except TimeoutError:
                    break

        await asyncio.gather(
            broker.find(types=["HTTP"], limit=limit),
            _drain(),
        )
        broker.stop()

        count = 0
        for proxy in grabbed:
            try:
                ip = proxy.host
                port = proxy.port
                types = getattr(proxy, "types", [])
                proto_str = types[0].name if types else "HTTP"
                protocol = ProxyProtocol.HTTP if "HTTP" in proto_str else (
                    ProxyProtocol.HTTPS if "HTTPS" in proto_str else ProxyProtocol.SOCKS5
                )
                try:
                    asn = await self._classifier.classify(ip)
                except Exception:
                    asn = "unknown"
                asn_class = (
                    AsnClass.RESIDENTIAL if "residential" in asn.lower()
                    else AsnClass.DATACENTER if "datacenter" in asn.lower()
                    else AsnClass.UNKNOWN
                )
                await self._pg.execute(
                    tenant,
                    """INSERT INTO proxy_pool
                       (ip, port, protocol, anonymity, asn_class)
                       VALUES ($1, $2, $3, $4, $5)
                       ON CONFLICT (ip, port) DO NOTHING""",
                    ip, port, protocol.value,
                    AnonymityLevel.ANONYMOUS.value,
                    asn_class.value,
                )
                count += 1
            except Exception:
                continue

        return count

    async def _direct_scrape(self, limit: int, tenant: TenantId) -> int:
        """Fallback: scrape proxy APIs directly when proxybroker2 returns none.

        BD-01: proxybroker2's provider modules have outdated parsing —
        upstream APIs return real proxy data but the library cannot parse them.
        This method bypasses proxybroker2 and parses the raw API responses.
        """
        if not re:  # keep import
            pass

        sources = [
            # proxyscrape API — returns clean IP:PORT, verified live
            (
                "https://api.proxyscrape.com/v2/"
                "?request=displayproxies&protocol=http&timeout=10000"
                "&country=all&ssl=all&anonymity=all",
                "ip_port",
            ),
            # proxyscrape HTTPS variant
            (
                "https://api.proxyscrape.com/v2/"
                "?request=displayproxies&protocol=https&timeout=10000"
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
