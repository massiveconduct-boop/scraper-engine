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
import time
from typing import TYPE_CHECKING, Any, Protocol

import httpx

from scraper_engine.core.models import AnonymityLevel, AsnClass, ProxyProtocol
from scraper_engine.proxy.net_probe import tcp_probe
from scraper_engine.proxy.scoring import ScoringEngine, compute_success_rate

if TYPE_CHECKING:
    from scraper_engine.core.tenant import TenantId
    from scraper_engine.storage.postgres_client import PostgresClient
    from scraper_engine.storage.redis_client import RedisClient

logger = logging.getLogger(__name__)

# A loopback judge (proxy/judge_server.py) cannot validate a real external
# proxy: "127.0.0.1" is resolved by whoever makes the request — the proxy
# itself, not us — so it always means "the proxy's own machine," never this
# one (found live, round 32: every validation failed via connection-refused
# from the proxy's side, confirmed by MiCGI/Tproxy response headers showing
# the proxy hitting its own local services instead of reaching us). Uses
# public IP-echo endpoints instead, rather than exposing our own stdlib
# http.server (no built-in timeout/request-size/slow-read protection — not
# meant for the open internet) to arbitrary third-party proxies.
# judge_server.py remains useful for fully offline/local testing
# (tests/unit/test_judge_server.py, tests/integration/test_promotion.py) but
# is no longer the production judge.
#
# Multiple independent, differently-hosted targets (round 32 follow-up) —
# httpbin.org itself was found live-down (persistent 503s, not transient)
# while building this fix, directly demonstrating why a single public
# service must never be a hard dependency for proxy scoring. Tried in
# order, first 200 wins (see _http_validate below); health_monitor.py's
# separate re-check loop imports this same list rather than keeping its own
# (that file previously declared a second URL as a "fallback" that was
# never actually used — same underlying bug, fixed together).
# Plain HTTP, deliberately — an HTTPS target routed through an HTTP forward
# proxy needs CONNECT tunneling, which many free HTTP-only proxies (and
# judge_server.py's own bare echo handler, used in tests) don't implement.
# All three confirmed to serve plain HTTP directly (no redirect-to-HTTPS).
JUDGE_URLS: tuple[str, ...] = (
    "http://httpbingo.org/ip",
    "http://api.ipify.org?format=json",
    "http://postman-echo.com/ip",
)
# Recognized body keys across the services above — "origin" (httpbingo),
# "ip" (ipify, postman-echo). "headers" kept for compatibility with
# judge_server.py's own echo shape (still used in offline tests).
_VALID_BODY_KEYS = ("origin", "ip", "headers")
HTTP_VALIDATE_TIMEOUT = 5.0
SCORE_TCP_ONLY = 25  # below L1 threshold (40)


class SupportsClassify(Protocol):
    """Anything that can classify an IP's ASN class (the harvester only needs this)."""

    async def classify(self, ip: str) -> str: ...


def _to_asn_class(raw: str) -> AsnClass:
    """Maps a classifier's free-text result to the AsnClass enum ScoringEngine
    expects. Shared by every write path (previously only _harvest_via_broker
    did this mapping; _scrape_one/promote_tcp_only discarded ASN entirely)."""
    lowered = raw.lower()
    if "residential" in lowered:
        return AsnClass.RESIDENTIAL
    if "mobile" in lowered:
        return AsnClass.MOBILE
    if "datacenter" in lowered:
        return AsnClass.DATACENTER
    return AsnClass.UNKNOWN


def _score_validation(
    latency_ms: int | None,
    anonymity: AnonymityLevel,
    asn: AsnClass,
    success_rate: float | None,
) -> float:
    """Score a fresh judge-validation reading.

    success_rate must be None only for a proxy genuinely new to the pool —
    pass the real compute_success_rate() of its existing global_success_count/
    global_failure_count for anything already in proxy_pool. Round 39: this
    was hardcoded to None for EVERY call, including re-harvesting an
    ip:port free proxy lists keep relisting, and promote_tcp_only()
    re-checking a proxy that had already earned real failures — since
    success_rate=None takes the "no track record" scoring branch (see
    scoring.py's compute_score docstring), which redistributes weight away
    from success_rate entirely, a fresh-but-history-blind score often came
    out well above a proxy's true decayed score. `_scrape_one`'s ON
    CONFLICT DO UPDATE used to take GREATEST(old, new) specifically so a
    noisy single bad reading couldn't drag a proven proxy down — but with
    success_rate always None, GREATEST let that same history-blind score
    silently ratchet a properly-decayed score back UP every re-harvest,
    undoing ProxyManager.mark_failure's real-time penalties, AND kept
    resetting last_validated to NOW() on every re-harvest, which starved
    health_monitor's oldest-last_validated-first correction query from
    ever reaching the row. Confirmed live: proxies with double-digit real
    failure counts carrying scores that only matched the zero-track-record
    formula, unchanged across multiple re-harvests after this fix landed,
    until GREATEST itself was also removed in favor of an unconditional
    overwrite — now that this reading's success_rate is real, the fresh
    score already reflects the truth and doesn't need protecting from
    itself."""
    return ScoringEngine().compute_score(
        latency_ms=latency_ms,
        success_rate=success_rate,
        anonymity=anonymity,
        asn=asn,
        last_validated_seconds_ago=0,
    ).total


class ProxyHarvester:
    def __init__(
        self,
        pg: PostgresClient,
        sources: list[str] | None = None,
        asn_classifier: SupportsClassify | None = None,
        redis: RedisClient | None = None,
    ) -> None:
        from scraper_engine.proxy.asn_classifier import NullAsnClassifier

        self._pg = pg
        self._sources = sources or []
        self._classifier: SupportsClassify = asn_classifier or NullAsnClassifier()
        # None disables the per-source health signal (round 25) — tests and
        # any other caller that doesn't need it can skip passing a redis client.
        self._redis = redis

    async def harvest_once(self, limit: int = 100) -> int:
        """Run one harvest cycle from both paths. Returns total proxies."""
        from scraper_engine.core.tenant import TenantId

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
            from scraper_engine.observability.metrics import proxy_pool_validated_count

            validated = await self._count_validated(system_tenant)
            proxy_pool_validated_count.set(validated)
        except Exception:
            logger.warning("proxy_pool_validated_count gauge update failed", exc_info=True)
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
        (
            "proxyscrape_http",
            "https://api.proxyscrape.com/v2/?request=displayproxies&protocol=http&timeout=10000&country=all&ssl=all&anonymity=all",
            "ip_port",
        ),
        (
            "proxyscrape_https",
            "https://api.proxyscrape.com/v2/?request=displayproxies&protocol=https&timeout=10000&country=all&ssl=all&anonymity=all",
            "ip_port",
        ),
        (
            "geonode",
            "https://proxylist.geonode.com/api/proxy-list?limit=100&page=1&sort_by=lastChecked&sort_type=desc&protocols=http%%2Chttps",
            "geonode_json",
        ),
        ("openproxylist", "https://api.openproxylist.xyz/http.txt", "ip_port"),
        (
            "thespeedx_github",
            "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt",
            "ip_port",
        ),
        (
            "monosans_github",
            "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/http.txt",
            "ip_port",
        ),
        ("pubproxy", "http://pubproxy.com/api/proxy?limit=100&format=txt", "ip_port"),
        (
            "proxyscrape_getproxies",
            "https://api.proxyscrape.com/?request=getproxies&proxytype=http",
            "ip_port",
        ),
        (
            "shiftytr_http",
            "https://raw.githubusercontent.com/ShiftyTR/Proxy-List/master/http.txt",
            "ip_port",
        ),
        (
            "shiftytr_https",
            "https://raw.githubusercontent.com/ShiftyTR/Proxy-List/master/https.txt",
            "ip_port",
        ),
        (
            "clarketm_github",
            "https://raw.githubusercontent.com/clarketm/proxy-list/master/proxy-list-raw.txt",
            "ip_port",
        ),
        (
            "sunny9577_github",
            "https://raw.githubusercontent.com/sunny9577/proxy-scraper/master/proxies.txt",
            "ip_port",
        ),
    ]

    # Round 38 — floor so a source late in SOURCES still gets a real quota
    # instead of `limit - total` degrading to ~0 once earlier sources already
    # filled the per-cycle budget. Root cause of pubproxy/proxyscrape_getproxies
    # having zero Redis source-health records after 2 days live (confirmed):
    # the old `if total >= limit: break` stopped the loop outright once any
    # earlier source alone hit `limit`, so sources ordered after it never ran
    # even once. Pool depth needs breadth (every source tried every cycle),
    # not just raw volume from whichever source answers first.
    MIN_PER_SOURCE = 10

    async def _direct_scrape(self, limit: int, tenant: TenantId) -> int:
        counts: dict[str, int] = {}
        total = 0
        async with httpx.AsyncClient(timeout=15) as client:
            for name, url, fmt in self.SOURCES:
                remaining = max(limit - total, self.MIN_PER_SOURCE)
                n = await self._scrape_one(name, url, fmt, remaining, tenant, client)
                counts[name] = n
                # Per-source health signal (round 13 D2) — a source going dark
                # becomes a specific named signal, not just a drop in the
                # aggregate. Written to Redis, not an in-process gauge (round
                # 25 — this process doesn't serve /metrics).
                if self._redis is not None:
                    from scraper_engine.proxy.source_health import record_source_health

                    await record_source_health(self._redis, name, n)
                total += n
        if any(counts.values()):
            logger.info(
                "harvest source breakdown: %s", ", ".join(f"{k}={v}" for k, v in counts.items())
            )
        return total

    async def _scrape_one(
        self, name: str, url: str, fmt: str, limit: int, tenant: TenantId, client: httpx.AsyncClient
    ) -> int:
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
            if not await tcp_probe(ip, port):
                continue
            # HTTP validation (proves proxy forwards traffic)
            is_valid, anonymity, latency_ms = await self._http_validate(ip, port, protocol)
            if is_valid:
                asn = _to_asn_class(await self._classifier.classify(ip))
                existing = await self._pg.fetchrow(
                    tenant,
                    """SELECT global_success_count, global_failure_count
                       FROM proxy_pool WHERE ip = $1 AND port = $2 AND protocol = $3""",
                    ip,
                    port,
                    protocol,
                )
                success_rate = (
                    compute_success_rate(
                        existing["global_success_count"], existing["global_failure_count"]
                    )
                    if existing is not None
                    else None
                )
                score = _score_validation(latency_ms, anonymity, asn, success_rate)
            else:
                asn = AsnClass.UNKNOWN
                score = SCORE_TCP_ONLY
            try:
                await self._pg.execute(
                    tenant,
                    """INSERT INTO proxy_pool
                           (ip, port, protocol, anonymity_level, asn_class, response_time_ms, reliability_score)
                       VALUES ($1,$2,$3,$4,$5,$6,$7)
                       ON CONFLICT (ip, port, protocol) DO UPDATE SET
                         reliability_score = EXCLUDED.reliability_score,
                         anonymity_level = EXCLUDED.anonymity_level,
                         asn_class = EXCLUDED.asn_class,
                         response_time_ms = EXCLUDED.response_time_ms,
                         last_validated = NOW()""",
                    ip,
                    port,
                    protocol,
                    anonymity.value,
                    asn.value,
                    latency_ms if is_valid else None,
                    score,
                )
                count += 1
                if count >= limit:
                    return count
            except Exception:
                continue
        return count

    # ── HTTP validation ─────────────────────────────────────────────────

    @staticmethod
    async def _http_validate(
        ip: str,
        port: int,
        protocol: str,
        timeout: float = HTTP_VALIDATE_TIMEOUT,
    ) -> tuple[bool, AnonymityLevel, int | None]:
        """Full HTTP round-trip through proxy to a judge endpoint.

        Returns (is_valid, anonymity_level, latency_ms). latency_ms is None
        when invalid, and — critically — measured only around the single
        request that actually succeeded, never around the whole JUDGE_URLS
        loop (round 38 fix: every caller used to wrap this entire call in
        its own `time.monotonic()` pair, so a dead/slow earlier candidate's
        full `timeout` got silently baked into the "proxy's latency" before
        a later candidate ever answered — proxies whose first-tried judge
        just happened to be unreachable were scored as if they took 5-15s
        to respond, when their real latency to the judge that DID answer
        was often under 2s. This crushed `latency_score` (the single
        heaviest-weighted dimension, 0.25-0.45 depending on formula branch)
        for a large fraction of the pool and was the dominant reason so few
        proxies ever crossed the L2 (70) threshold regardless of how many
        harvest sources fed the pool — confirmed live: `response_time_ms`
        values clustering just above multiples of `HTTP_VALIDATE_TIMEOUT`
        (5000ms) across the real pool, e.g. 6805ms/11878ms, consistent with
        1-2 failed judge attempts eating their full timeout before a later
        one answered in a more ordinary ~1-2s).

        Tries JUDGE_URLS in order, stopping at the first that returns a
        real 200 with a recognized body — one endpoint's outage (httpbin.org
        was found live-down while building this) no longer looks like every
        proxy in the pool failing. Worst case (every candidate unreachable)
        is timeout * len(JUDGE_URLS); acceptable since this only runs from
        already-bounded-concurrency contexts (harvest is sequential,
        ProxyPromotionJob caps concurrent validations at
        PROMOTION_CONCURRENCY), never a request-path hot loop.
        """
        proxy_url = f"{protocol.lower()}://{ip}:{port}"
        try:
            async with httpx.AsyncClient(
                proxy=proxy_url,
                timeout=timeout,
                follow_redirects=False,
            ) as client:
                resp = None
                latency_ms: int | None = None
                for url in JUDGE_URLS:
                    attempt_start = time.monotonic()
                    try:
                        candidate = await client.get(url)
                    except Exception:
                        continue
                    if candidate.status_code != 200:
                        continue
                    try:
                        data = candidate.json()
                    except Exception:
                        continue
                    if any(key in data for key in _VALID_BODY_KEYS):
                        resp = candidate
                        latency_ms = int((time.monotonic() - attempt_start) * 1000)
                        break
                if resp is None:
                    return False, AnonymityLevel.TRANSPARENT, None
        except Exception:
            return False, AnonymityLevel.TRANSPARENT, None

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

        return True, level, latency_ms

    # ── parsers ──────────────────────────────────────────────────────────

    @staticmethod
    def _parse_ip_port(text: str, limit: int) -> list[tuple[str, int, str]]:
        result = []
        for line in text.split("\n")[: limit * 2]:
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
    def _parse_geonode(data: dict[str, Any], limit: int) -> list[tuple[str, int, str]]:
        result = []
        for entry in data.get("data", [])[: limit * 2]:
            ip = entry.get("ip", "")
            port = entry.get("port", 0)
            protocols = entry.get("protocols", [])
            proto = "HTTP" if "http" in protocols else ("HTTPS" if "https" in protocols else "HTTP")
            if ip and port and re.match(r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$", ip):
                result.append((ip, int(port), proto))
        return result

    # ── proxybroker2 subprocess fallback ─────────────────────────────────

    async def _harvest_via_broker(self, limit: int, tenant: TenantId) -> int:
        provider_list = (
            self._sources
            if self._sources
            else [
                "https://api.proxyscrape.com/?request=getproxies&proxytype=http",
            ]
        )
        providers_repr = "[" + ",".join(repr(p) for p in provider_list) + "]"
        script = f"""import asyncio, json
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
asyncio.run(main())"""

        fd, path = tempfile.mkstemp(suffix=".py")
        try:
            with os.fdopen(fd, "w") as f:
                f.write(script)
            env = os.environ.copy()
            env["VIRTUAL_ENV"] = os.path.dirname(os.path.dirname(sys.executable))
            env["PATH"] = os.path.dirname(sys.executable) + ":" + env.get("PATH", "")
            proc = await asyncio.create_subprocess_exec(
                sys.executable,
                path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
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
            logger.warning(
                "proxybroker2 subprocess failed (rc=%d): %s", proc.returncode, err[-500:]
            )
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
                protocol = (
                    ProxyProtocol.HTTP
                    if "HTTP" in proto_str
                    else (ProxyProtocol.HTTPS if "HTTPS" in proto_str else ProxyProtocol.SOCKS5)
                )
                try:
                    asn = await self._classifier.classify(ip)
                except Exception:
                    asn = "unknown"
                asn_class = _to_asn_class(asn)
                # proxybroker2 validates the proxy against ITS OWN judge
                # providers, not ours, and never tells us the real anonymity
                # level — previously assumed ANONYMOUS blindly. Run our own
                # _http_validate() too (round 32 — same treatment as
                # _scrape_one) so broker-sourced proxies get a real,
                # comparable score instead of a guessed one. Low extra cost:
                # this path is documented as low-volume (1-5 proxies/cycle).
                is_valid, anonymity, latency_ms = await self._http_validate(
                    ip, port, protocol.value
                )
                if not is_valid:
                    continue
                existing = await self._pg.fetchrow(
                    tenant,
                    """SELECT global_success_count, global_failure_count
                       FROM proxy_pool WHERE ip = $1 AND port = $2 AND protocol = $3""",
                    ip,
                    port,
                    protocol.value,
                )
                success_rate = (
                    compute_success_rate(
                        existing["global_success_count"], existing["global_failure_count"]
                    )
                    if existing is not None
                    else None
                )
                score = _score_validation(latency_ms, anonymity, asn_class, success_rate)
                await self._pg.execute(
                    tenant,
                    """INSERT INTO proxy_pool
                           (ip, port, protocol, anonymity_level, asn_class, response_time_ms, reliability_score)
                       VALUES ($1,$2,$3,$4,$5,$6,$7)
                       ON CONFLICT (ip, port, protocol) DO UPDATE SET
                         reliability_score = EXCLUDED.reliability_score,
                         anonymity_level = EXCLUDED.anonymity_level,
                         asn_class = EXCLUDED.asn_class,
                         response_time_ms = EXCLUDED.response_time_ms,
                         last_validated = NOW()""",
                    ip,
                    port,
                    protocol.value,
                    anonymity.value,
                    asn_class.value,
                    latency_ms,
                    score,
                )
                count += 1
            except Exception:
                continue
        return count

    # ── background promotion ──────────────────────────────────────────

    async def promote_tcp_only(self, limit: int = 50, tenant: TenantId | None = None) -> int:
        """Promote TCP-only proxies (score=25) to validated (score=60).

        Re-checks proxies with reliability_score < 40 via HTTP validator.
        Called on a schedule by the harvester daemon.
        """
        from scraper_engine.core.tenant import TenantId

        if tenant is None:
            tenant = TenantId("system")
        rows = await self._pg.fetch(
            tenant,
            """SELECT ip, port, protocol, global_success_count, global_failure_count
               FROM proxy_pool
               WHERE reliability_score < 40
               ORDER BY last_promotion_attempt_at ASC NULLS FIRST
               LIMIT $1""",
            limit,
        )
        promoted = 0
        for row in rows:
            ip, port, protocol = row["ip"], row["port"], row["protocol"]
            is_valid, anonymity, latency_ms = await self._http_validate(ip, port, protocol)
            if is_valid:
                asn = _to_asn_class(await self._classifier.classify(ip))
                success_rate = compute_success_rate(
                    row["global_success_count"], row["global_failure_count"]
                )
                score = _score_validation(latency_ms, anonymity, asn, success_rate)
                await self._pg.execute(
                    tenant,
                    """UPDATE proxy_pool
                       SET reliability_score = $1, anonymity_level = $2,
                           asn_class = $3, response_time_ms = $4,
                           promotion_attempts = promotion_attempts + 1,
                           last_promotion_attempt_at = NOW()
                       WHERE ip = $5 AND port = $6 AND protocol = $7""",
                    score,
                    anonymity.value,
                    asn.value,
                    latency_ms,
                    ip,
                    port,
                    protocol,
                )
                promoted += 1
            else:
                await self._pg.execute(
                    tenant,
                    """UPDATE proxy_pool
                       SET promotion_attempts = promotion_attempts + 1,
                           last_promotion_attempt_at = NOW()
                       WHERE ip = $3 AND port = $4 AND protocol = $5""",
                    ip,
                    port,
                    protocol,
                )
        return promoted
