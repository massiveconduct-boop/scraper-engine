# proxy/net_probe.py
"""Shared fast liveness checks — used by both the harvester's candidate
pre-filter (proxy/harvester.py) and ProxyManager's lease-time preflight
(proxy/manager.py), so a single set of connect implementations backs both.
"""

from __future__ import annotations

import asyncio

import httpx

# HTTPS, deliberately -- not harvester.py's plain-HTTP JUDGE_URLS. A proxy
# that forwards plain HTTP fine can still fail an HTTPS CONNECT tunnel
# (some free proxies do plain relaying only, no CONNECT support), and
# almost everything L2/L3 actually needs a proxy for IS https: real target
# pages, and Camoufox's own geoip=True launch-time IP lookup (config.
# camoufox.geoip, default True, wired into the real BrowserPool in
# orchestrator/tasks.py) — both go out over HTTPS. A plain-HTTP check
# passed proxies that then failed those with a CONNECT tunnel error,
# caught live (round 37): "Unable to connect to proxy ... Tunnel
# connection failed: 400 Bad Request" from Camoufox's own geoip dial,
# despite the proxy passing an HTTP-only preflight moments earlier. Also
# deliberately NOT harvester.py's full JUDGE_URLS retry list —
# _http_validate there is documented as unsuitable for a request-path hot
# loop ("this only runs from already-bounded-concurrency contexts... never
# a request-path hot loop"); this is a lighter, purpose-built check for
# exactly that hot loop, with no anonymity classification and no
# multi-URL retry.
_LEASE_CHECK_URL = "https://httpbingo.org/ip"


async def tcp_probe(ip: str, port: int, timeout: float = 2.0) -> bool:
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(ip, port), timeout=timeout)
        writer.close()
        await writer.wait_closed()
        return True
    except Exception:
        return False


async def http_probe(ip: str, port: int, protocol: str, timeout: float = 2.0) -> bool:
    """One real GET of an HTTPS URL through the proxy — httpx issues this as
    a CONNECT tunnel. Catches two distinct live-caught failure modes: (1) a
    proxy that accepts a TCP connection but doesn't actually forward
    traffic at all ("Connection to remote host was lost" mid-navigation),
    and (2) a proxy that forwards plain HTTP fine but can't do HTTPS
    CONNECT tunneling ("Tunnel connection failed: 400 Bad Request") — the
    second is the more consequential case, since real target pages and
    Camoufox's own geoip launch check are both HTTPS. See
    technical-debt.md's round-37 entry for the live evidence behind both."""
    proxy_url = f"{protocol.lower()}://{ip}:{port}"
    try:
        async with httpx.AsyncClient(
            proxy=proxy_url, timeout=timeout, follow_redirects=False
        ) as client:
            resp = await client.get(_LEASE_CHECK_URL)
            return resp.status_code == 200
    except Exception:
        return False


async def lease_preflight(ip: str, port: int, protocol: str, timeout: float = 2.0) -> bool:
    """Combined check run before a proxy is leased for a real fetch: cheap
    TCP reject first (catches the dominant ConnectTimeout/ConnectError
    failure mode in ~2s), then one real HTTP round trip only if the TCP
    check passed (catches the smaller "connects but doesn't forward"
    residual). Worst case per candidate is bounded (2 * timeout) instead of
    the full browser navigation timeout a bad lease used to cost."""
    if not await tcp_probe(ip, port, timeout):
        return False
    return await http_probe(ip, port, protocol, timeout)
