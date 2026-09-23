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
# despite the proxy passing an HTTP-only preflight moments earlier.
#
# Round 39 — three HTTPS judges, not one, first success wins. A single
# judge with no fallback (httpbingo.org alone, previously) meant that
# site's own transient flakiness/rate-limiting looked identical to the
# proxy being dead, false-negativing genuinely-working proxies straight
# into mark_failure — the same category of bug harvester.py's own
# multi-judge JUDGE_URLS already guards against, just not mirrored here
# yet. Still deliberately NOT a straight reuse of harvester.py's
# JUDGE_URLS: those are plain-HTTP (wrong protocol for this specific
# check, see above) and that whole loop is documented as unsuitable for a
# request-path hot loop; these are HTTPS equivalents of the same three
# hosts, kept to a small, purpose-built list with no anonymity
# classification. Worst case per candidate grows from one judge's timeout
# to up to three (first-success-wins, so the common case — any judge
# healthy — is unaffected).
_LEASE_CHECK_URLS: tuple[str, ...] = (
    "https://httpbingo.org/ip",
    "https://api.ipify.org?format=json",
    "https://postman-echo.com/ip",
)


async def tcp_probe(ip: str, port: int, timeout: float = 2.0) -> bool:
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(ip, port), timeout=timeout)
        writer.close()
        await writer.wait_closed()
        return True
    except Exception:
        return False


async def http_probe(ip: str, port: int, protocol: str, timeout: float = 4.0) -> bool:
    """One real GET of an HTTPS URL through the proxy — httpx issues this as
    a CONNECT tunnel. Catches two distinct live-caught failure modes: (1) a
    proxy that accepts a TCP connection but doesn't actually forward
    traffic at all ("Connection to remote host was lost" mid-navigation),
    and (2) a proxy that forwards plain HTTP fine but can't do HTTPS
    CONNECT tunneling ("Tunnel connection failed: 400 Bad Request") — the
    second is the more consequential case, since real target pages and
    Camoufox's own geoip launch check are both HTTPS. See
    technical-debt.md's round-37 entry for the live evidence behind both.

    Default raised 2.0s -> 4.0s (round 38, second pass): confirmed live
    that this fixed 2.0s budget was rejecting proxies with a real,
    accurately-measured (post round-38 harvester._http_validate fix) judge
    round-trip of 1.9-3.2s — proxies the scoring system had just correctly
    promoted into L2/L3 range were then failing the lease-time preflight
    purely on a too-tight clock, not real unreliability, and getting
    punished via mark_failure for it. Watched this collapse a fresh 43
    L2-caliber / 5 L3-caliber pool back to 0/0 within about 75 minutes of
    real traffic — every one of the 5 original L3 proxies had 0 recorded
    successes and 3-11 preflight-driven failures, judge-latencies of
    607-3242ms.

    Round 39 — tries every URL in _LEASE_CHECK_URLS, first 200 wins,
    instead of a single judge with no fallback. One flaky/rate-limited
    judge previously looked identical to a dead proxy — a real, working
    proxy that just happened to hit that judge's own bad moment got
    false-negatived straight into mark_failure, same failure shape as the
    too-tight-timeout bug this docstring already documents above."""
    proxy_url = f"{protocol.lower()}://{ip}:{port}"
    try:
        async with httpx.AsyncClient(
            proxy=proxy_url, timeout=timeout, follow_redirects=False
        ) as client:
            for check_url in _LEASE_CHECK_URLS:
                try:
                    resp = await client.get(check_url)
                    if resp.status_code == 200:
                        return True
                except Exception:
                    continue
            return False
    except Exception:
        return False


async def lease_preflight(ip: str, port: int, protocol: str, timeout: float = 4.0) -> bool:
    """Combined check run before a proxy is leased for a real fetch: cheap
    TCP reject first (catches the dominant ConnectTimeout/ConnectError
    failure mode fast — a refused/unroutable connection fails in
    milliseconds regardless of the timeout ceiling), then a real HTTP
    round trip only if the TCP check passed (catches the smaller "connects
    but doesn't forward" residual).

    Default raised 2.0s -> 4.0s, see http_probe's docstring for the live
    evidence behind that. Round 39 raised http_probe from one judge to
    three (first-success-wins) — worst case per candidate is now bounded
    at (1 + 3) * timeout = 20.0s (was 2*4.0=8.0s), only hit if TCP
    connects but every one of three independent judges simultaneously
    times out for this specific proxy; the common cases (proxy dead at
    TCP, or the first judge answers) are unaffected. Worst case across
    ProxyManager.MAX_ATTEMPTS=10 candidates is now up to 200s — large in
    the theoretical worst case, but that worst case requires the rare
    triple-judge-timeout on every single one of 10 candidates; the
    practical case (round 37's original problem: a single bad lease
    costing a full 40-60s browser navigation timeout with NO preflight at
    all) is what this bounds, and still does. Accepted trade-off:
    correctly-scored-but-moderately-slow real proxies, and proxies whose
    only problem is one flaky judge, actually getting a fair chance
    matters more here than shaving the theoretical worst case."""
    if not await tcp_probe(ip, port, timeout):
        return False
    return await http_probe(ip, port, protocol, timeout)
