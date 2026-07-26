# ruff: noqa: E501  -- SOURCES tuples + subprocess script strings
# proxy/harvester.py
"""Proxy harvester — multi-source discovery with HTTP validation.

6+ independent upstream sources per round-6 directive:
  proxyscrape (HTTP+HTTPS), geonode, openproxylist.xyz,
  TheSpeedX (GitHub), monosans (GitHub)
HTTP round-trip validation via judge endpoint. Two-tier scoring.
"""

from __future__ import annotations

import asyncio
import contextlib
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

JUDGE_URL = "http://127.0.0.1:8089/"  # self-hosted judge (judge_server.py)
HTTP_VALIDATE_TIMEOUT = 5.0
SCORE_TCP_ONLY = 25  # below L1 threshold (40)
SCORE_VALIDATED = 60  # above L1 threshold


class FakeClassifier:
    async def classify(self, ip: str) -> str:
        return "unknown"


class ProxyHarvester:
    def __init__(
        self, pg: PostgresClient,
        sources: list[str] | None = None,
        asn_classifier: object | None = None,
    ) -> None:
        self._pg = pg
        self._sources = sources or []
        self._classifier = asn_classifier or FakeClassifier()

    async def harvest_once(self, limit: int = 100) -> int:
        """Run one harvest cycle from both paths. Returns total proxies."""
        from core.tenant import TenantId
        system_tenant = TenantId("system")
        count = await self._direct_scrape(limit, system_tenant)
        if count < limit:
            try:
                broker_count = await self._harvest_via_broker(max(limit - count, 5), system_tenant)
                count += broker_count
            except Exception as exc:
                logger.warning("proxybroker2 harvest failed: %s", exc)

        # Update Prometheus gauge with validated proxy count
        try:
            from observability.metrics import proxy_pool_validated_count
            validated = await self._count_validated(system_tenant)
            proxy_pool_validated_count.set(validated)
        except Exception:
            pass
        return count

    async def _count_validated(self, tenant: TenantId) -> int:
        """Count proxies with reliability_score >= 40 (L1 threshold)."""
        rows = await self._pg.fetch(
            tenant,
            "SELECT COUNT(*) as n FROM proxy_pool WHERE reliability_score >= 40",
        )
        return rows[0]["n"] if rows else 0

    # ── direct multi-source scrape ──────────────────────────────────────

    # All sources parse to ip_port format (IP:PORT per line), except geonode_json.
    SOURCES = [
        ("proxyscrape_http", "https://api.proxyscrape.com/v2/?request=displayproxies&protocol=http&timeout=10000&country=all&ssl=all&anonymity=all", "ip_port"),
        ("proxyscrape_https", "https://api.proxyscrape.com/v2/?request=displayproxies&protocol=https&timeout=10000&country=all&ssl=all&anonymity=all", "ip_port"),
        ("geonode", "https://proxylist.geonode.com/api/proxy-list?limit=100&page=1&sort_by=lastChecked&sort_type=desc&protocols=http%%2Chttps", "geonode_json"),
        ("openproxylist", "https://api.openproxylist.xyz/http.txt", "ip_port"),
        ("thespeedx_github", "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt", "ip_port"),
        ("monosans_github", "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/http.txt", "ip_port"),
        ("pubproxy", "http://pubproxy.com/api/proxy?limit=100&format=txt", "ip_port"),
        ("proxyscrape_getproxies", "https://api.proxyscrape.com/?request=getproxies&proxytype=http", "ip_port"),
    ]

    async def _direct_scrape(self, limit: int, tenant: TenantId) -> int:
        counts: dict[str, int] = {}
        total = 0
        async with httpx.AsyncClient(timeout=15) as client:
            for name, url, fmt in self.SOURCES:
                n = await self._scrape_one(name, url, fmt, limit - total, tenant, client)
                counts[name] = n
                total += n
                if total >= limit:
                    break
        if any(counts.values()):
            logger.info("harvest source breakdown: %s",
                        ", ".join(f"{k}={v}" for k, v in counts.items()))
        return total

    async def _scrape_one(self, name: str, url: str, fmt: str,
                          limit: int, tenant: TenantId,
                          client: httpx.AsyncClient) -> int:
        try:
            resp = await client.get(url)
            resp.raise_for_status()
        except Exception as exc:
            logger.warning("%s: fetch failed — %s", name, exc)
            return 0

        if fmt == "ip_port":
            proxies = self._parse_ip_port(resp.text, limit)
        elif fmt == "geonode_json":
            proxies = self._parse_geonode(resp.json(), limit)
        else:
            return 0

        count = 0
        for ip, port, protocol in proxies:
            # TCP probe (fast)
            if not await self._tcp_probe(ip, port):
                continue
            # HTTP validation (proves proxy forwards traffic)
            is_valid, anonymity = await self._http_validate(ip, port, protocol)
            score = SCORE_VALIDATED if is_valid else SCORE_TCP_ONLY
            try:
                await self._pg.execute(
                    tenant,
                    """INSERT INTO proxy_pool (ip, port, protocol, anonymity_level, asn_class, reliability_score)
                       VALUES ($1,$2,$3,$4,$5,$6)
                       ON CONFLICT (ip, port, protocol) DO UPDATE SET
                         reliability_score = GREATEST(proxy_pool.reliability_score, EXCLUDED.reliability_score),
                         anonymity_level = CASE WHEN EXCLUDED.reliability_score > proxy_pool.reliability_score
                           THEN EXCLUDED.anonymity_level ELSE proxy_pool.anonymity_level END,
                         last_validated = NOW()""",
                    ip, port, protocol, anonymity.value, "unknown", score,
                )
                count += 1
                if count >= limit:
                    return count
            except Exception:
                continue
        return count

    # ── HTTP validation ─────────────────────────────────────────────────

    @staticmethod
    async def _http_validate(ip: str, port: int, protocol: str,
                             timeout: float = HTTP_VALIDATE_TIMEOUT,
                             ) -> tuple[bool, AnonymityLevel]:
        """Full HTTP round-trip through proxy to judge endpoint.

        Returns (is_valid, anonymity_level).
        Never returns True on TCP-connect success alone.
        """
        proxy_url = f"{protocol.lower()}://{ip}:{port}"
        try:
            async with httpx.AsyncClient(
                proxy=proxy_url, timeout=timeout, follow_redirects=False,
            ) as client:
                resp = await client.get(JUDGE_URL)
                if resp.status_code != 200:
                    return False, AnonymityLevel.TRANSPARENT
                data = resp.json()
                if "headers" not in data and "origin" not in data:
                    return False, AnonymityLevel.TRANSPARENT
        except Exception:
            return False, AnonymityLevel.TRANSPARENT

        # Classify anonymity from response headers
        via = resp.headers.get("Via", "")
        xff = resp.headers.get("X-Forwarded-For", "")
        proxy_conn = resp.headers.get("Proxy-Connection", "")

        if not via and not xff and not proxy_conn:
            level = AnonymityLevel.ELITE
        elif not xff:
            level = AnonymityLevel.ANONYMOUS
        else:
            level = AnonymityLevel.TRANSPARENT

        return True, level

    # ── parsers ──────────────────────────────────────────────────────────

    @staticmethod
    def _parse_ip_port(text: str, limit: int) -> list[tuple[str, int, str]]:
        result = []
        for line in text.split("\n")[:limit * 2]:
            line = line.strip()
            if not line or ":" not in line:
                continue
            try:
                ip, port_str = line.rsplit(":", 1)
                port = int(port_str)
            except ValueError:
                continue
            if not re.match(r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$", ip):
                continue
            result.append((ip, port, "HTTP"))
        return result

    @staticmethod
    def _parse_geonode(data: dict, limit: int) -> list[tuple[str, int, str]]:
        result = []
        for entry in data.get("data", [])[:limit * 2]:
            ip = entry.get("ip", "")
            port = entry.get("port", 0)
            protocols = entry.get("protocols", [])
            proto = "HTTP" if "http" in protocols else ("HTTPS" if "https" in protocols else "HTTP")
            if ip and port and re.match(r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$", ip):
                result.append((ip, int(port), proto))
        return result

    # ── TCP probe (fast pre-filter) ──────────────────────────────────────

    @staticmethod
    async def _tcp_probe(ip: str, port: int, timeout: float = 2.0) -> bool:
        try:
            _, writer = await asyncio.wait_for(asyncio.open_connection(ip, port), timeout=timeout)
            writer.close()
            await writer.wait_closed()
            return True
        except Exception:
            return False

    # ── proxybroker2 subprocess fallback ─────────────────────────────────

    async def _harvest_via_broker(self, limit: int, tenant: TenantId) -> int:
        provider_list = self._sources if self._sources else [
            "https://api.proxyscrape.com/?request=getproxies&proxytype=http",
        ]
        providers_repr = "[" + ",".join(repr(p) for p in provider_list) + "]"
        script = f'''import asyncio, json
from proxybroker2 import Broker
async def main():
    q=asyncio.Queue()
    b=Broker(q,providers={providers_repr},timeout=15,max_conn=50,max_tries=1,verify_ssl=False)
    r=[]
    async def d():
        while len(r)<{limit}:
            try:
                p=await asyncio.wait_for(q.get(),timeout=30)
                if p is None:break
                r.append({{"host":p.host,"port":p.port,"types":[str(t) for t in p.types]if p.types else["HTTP"]}})
            except TimeoutError:break
    await asyncio.gather(b.find(types=["HTTP","HTTPS"],limit={limit}),d())
    b.stop()
    print(json.dumps(r))
asyncio.run(main())'''

        fd, path = tempfile.mkstemp(suffix=".py")
        try:
            with os.fdopen(fd, "w") as f:
                f.write(script)
            env = os.environ.copy()
            env["VIRTUAL_ENV"] = os.path.dirname(os.path.dirname(sys.executable))
            env["PATH"] = os.path.dirname(sys.executable) + ":" + env.get("PATH", "")
            proc = await asyncio.create_subprocess_exec(
                sys.executable, path, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, env=env,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
        except TimeoutError:
            logger.warning("proxybroker2 subprocess timed out")
            return 0
        finally:
            with contextlib.suppress(OSError):
                os.unlink(path)

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
                ip, port = pdata["host"], pdata["port"]
                proto_str = pdata["types"][0] if pdata["types"] else "HTTP"
                protocol = ProxyProtocol.HTTP if "HTTP" in proto_str else (
                    ProxyProtocol.HTTPS if "HTTPS" in proto_str else ProxyProtocol.SOCKS5)
                try:
                    asn = await self._classifier.classify(ip)
                except Exception:
                    asn = "unknown"
                asn_class = (AsnClass.RESIDENTIAL if "residential" in asn.lower()
                             else AsnClass.DATACENTER if "datacenter" in asn.lower()
                             else AsnClass.UNKNOWN)
                await self._pg.execute(tenant,
                    """INSERT INTO proxy_pool (ip, port, protocol, anonymity_level, asn_class, reliability_score)
                       VALUES ($1,$2,$3,$4,$5,$6)
                       ON CONFLICT (ip, port, protocol) DO UPDATE SET
                         reliability_score = GREATEST(proxy_pool.reliability_score, EXCLUDED.reliability_score),
                         anonymity_level = CASE WHEN EXCLUDED.reliability_score > proxy_pool.reliability_score
                           THEN EXCLUDED.anonymity_level ELSE proxy_pool.anonymity_level END,
                         last_validated = NOW()""",
                    ip, port, protocol.value, AnonymityLevel.ANONYMOUS.value, asn_class.value, SCORE_VALIDATED)
                count += 1
            except Exception:
                continue
        return count

    # ── background promotion ──────────────────────────────────────────

    async def promote_tcp_only(self, limit: int = 50, tenant: TenantId = None) -> int:
        """Promote TCP-only proxies (score=25) to validated (score=60).

        Re-checks proxies with reliability_score < 40 via HTTP validator.
        Called on a schedule by the harvester daemon.
        """
        from core.tenant import TenantId
        if tenant is None:
            tenant = TenantId("system")
        rows = await self._pg.fetch(
            tenant,
            """SELECT ip, port, protocol FROM proxy_pool
               WHERE reliability_score < 40
               ORDER BY last_promotion_attempt_at ASC NULLS FIRST
               LIMIT $1""",
            limit,
        )
        promoted = 0
        for row in rows:
            ip, port, protocol = row["ip"], row["port"], row["protocol"]
            is_valid, anonymity = await self._http_validate(ip, port, protocol)
            if is_valid:
                await self._pg.execute(
                    tenant,
                    """UPDATE proxy_pool
                       SET reliability_score = $1, anonymity_level = $2,
                           promotion_attempts = promotion_attempts + 1,
                           last_promotion_attempt_at = NOW()
                       WHERE ip = $3 AND port = $4 AND protocol = $5""",
                    SCORE_VALIDATED, anonymity.value, ip, port, protocol,
                )
                promoted += 1
            else:
                await self._pg.execute(
                    tenant,
                    """UPDATE proxy_pool
                       SET promotion_attempts = promotion_attempts + 1,
                           last_promotion_attempt_at = NOW()
                       WHERE ip = $3 AND port = $4 AND protocol = $5""",
                    ip, port, protocol,
                )
        return promoted
