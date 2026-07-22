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
        proxy_stream = await broker.find(limit=limit, types=["HTTP", "HTTPS"])
        if proxy_stream is None:
            logger.warning("proxy sources returned no results — sources may be unreachable")
            return 0
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
