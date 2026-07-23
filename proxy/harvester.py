# proxy/harvester.py
"""Proxy harvester — discovers, validates, and persists free proxies.

Primary: direct API scraping (fast, reliable).
Secondary: proxybroker2 subprocess (validated, slower).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
import tempfile
from typing import TYPE_CHECKING

import httpx

from core.models import AnonymityLevel, AsnClass, ProxyProtocol

if TYPE_CHECKING:
    from core.tenant import TenantId
    from storage.postgres_client import PostgresClient

logger = logging.getLogger(__name__)


class FakeClassifier:
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
        """Run one harvest cycle. Returns count of newly persisted proxies."""
        from core.tenant import TenantId

        system_tenant = TenantId("system")

        # Primary: direct API scraping (fast, reliable, no dependencies)
        count = await self._direct_scrape(limit, system_tenant)
        if count > 0:
            return count

        # Fallback: proxybroker2 (validated, uses aiohttp event loop)
        try:
            count = await self._harvest_via_broker(limit, system_tenant)
        except Exception as exc:
            logger.warning("proxybroker2 harvest failed: %s", exc)

        return count

    async def _harvest_via_broker(self, limit: int, tenant: TenantId) -> int:
        """proxybroker2 via subprocess — isolates aiohttp from httpx event loop."""
        provider = (
            self._sources[0] if self._sources
            else "https://api.proxyscrape.com/?request=getproxies&proxytype=http"
        )

        script = f'''import asyncio, json
from proxybroker2 import Broker
async def main():
    q=asyncio.Queue()
    b=Broker(q,providers=[{provider!r}],timeout=15,max_conn=10,max_tries=1,verify_ssl=False)
    r=[]
    async def d():
        while len(r)<{limit}:
            try:
                p=await asyncio.wait_for(q.get(),timeout=60)
                if p is None:break
                r.append({{"host":p.host,"port":p.port,"types":[str(t) for t in p.types]if p.types else["HTTP"]}})
            except TimeoutError:break
    await asyncio.gather(b.find(types=["HTTP"],limit={limit}),d())
    b.stop()
    print(json.dumps(r))
asyncio.run(main())'''

        fd, path = tempfile.mkstemp(suffix=".py")
        try:
            with os.fdopen(fd, "w") as f:
                f.write(script)

            venv_python = sys.executable
            env = os.environ.copy()
            env["VIRTUAL_ENV"] = os.path.dirname(os.path.dirname(sys.executable))
            env["PATH"] = os.path.dirname(sys.executable) + ":" + env.get("PATH", "")
            proc = await asyncio.create_subprocess_exec(
                venv_python, path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=90)
        except TimeoutError:
            logger.warning("proxybroker2 subprocess timed out")
            return 0
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass

        if proc.returncode != 0:
            err = stderr.decode() if stderr else "no stderr"
            logger.warning("proxybroker2 subprocess failed (rc=%d): %s", proc.returncode, err[-500:])
            return 0

        try:
            proxies = json.loads(stdout.decode())
        except json.JSONDecodeError as exc:
            logger.warning("proxybroker2 JSON parse failed: %s", exc)
            return 0

        count = 0
        for pdata in proxies:
            try:
                ip = pdata["host"]
                port = pdata["port"]
                proto_str = pdata["types"][0] if pdata["types"] else "HTTP"
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
        """Direct API scraping — fast, no proxybroker2 dependency."""
        sources = [
            (
                "https://api.proxyscrape.com/v2/"
                "?request=displayproxies&protocol=http&timeout=10000"
                "&country=all&ssl=all&anonymity=all",
                "ip_port",
            ),
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
