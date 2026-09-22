# proxy/paid_gateway.py
"""Paid rotating-gateway residential proxy provider (DataImpulse), round 40.

Architecturally distinct from proxy/manager.py's free-pool model: a rotating
gateway is one always-up host:port with server-side IP rotation per
connection, not a scored, individually-tracked ip:port row in proxy_pool.
This module deliberately does NOT touch the database, scoring, or
lease_preflight — see orchestrator/worker.py::_fetch_with_proxy's strategy
branch for why (Proxy.source == "paid_gateway" is checked there to skip
pool-only bookkeeping).

Credentials follow the same convention as services/captcha_solver.py's
build_captcha_solver: read directly via os.environ.get(), never modeled in
config/schema.py or base.yaml (no pydantic-settings/SecretStr in this repo).

Round 62 — exit-IP selection. DataImpulse encodes targeting and session
pinning as parameters appended to the *username*, not as separate fields:

    login__cr.au;sessid.45:password@gw.dataimpulse.com:823

(see https://docs.dataimpulse.com/proxies/parameters/session-id and
.../country). The rules that matter here, straight from those pages:

  * parameters start after a DOUBLE underscore, are separated by `;`, and
    each one is `key.value` — NOT `;key=value`. A wrong separator is what
    produces the gateway's `407 NO_USER` rejection.
  * `cr.<iso2>` pins the exit country.
  * `asn.<number>` pins the exit to one autonomous system (bare number, no
    "AS" prefix). Billed at DOUBLE the standard rate, per DataImpulse's own
    docs — it is an opt-in lever for a specific hard target, never a global
    default. See below for why it is the one that actually works.
  * `sessid.<label>` pins ONE exit IP to that label for ~30 minutes. A
    label never used before gets a fresh IP; reusing a label gets the same
    IP back.

That last point is the whole reason this module grew `session_id`: without
a sessid the gateway rotates on its own schedule, which is not something a
caller can force. When Cloudflare flags the current exit IP, "try again"
has to mean "try again from a DIFFERENT IP", and the only way to say that
to DataImpulse is to present a session label it has not seen. See
orchestrator/worker.py::_fetch_with_proxy, which calls new_session_id()
once per attempt for exactly this.

Rotation alone is a lottery, though, and measuring the odds found the real
problem. 12 fresh Nigerian exit IPs against the Jumia catalog URL from
DEVELOPER_REPORT.md, one real Camoufox render each, 20 Sep 2026:

    AS37127  Visafone Communications    ok=0  blocked=9
    AS29465  MTN Nigeria                ok=1  blocked=0
    AS36873  Airtel Networks            ok=1  blocked=0
    AS328555 Timeless Network Services  ok=1  blocked=0

The ~10% success rate was not IP reputation scattered across the pool. One
ASN makes up most of DataImpulse's Nigerian residential pool and Cloudflare
blocks all of it; every other ASN sailed through.

The documented `noasn.<n>` exclusion parameter looked like the obvious fix
and is NOT usable: on this account it authenticates fine but is silently
ignored, and AS37127 still came back 8 times out of 12 with
`__cr.ng;noasn.37127` set. Positive targeting does work — `__cr.ng;asn.29465`
returned MTN 10 times out of 10 — so `asn` is what this module exposes.
Re-running the blocked Jumia fetch through a pinned good ASN:

    asn.29465 (MTN)     ok=6 blocked=0
    asn.36873 (Airtel)  ok=6 blocked=0

12 of 12, against 1 of 12 unpinned. That is the fix; rotation below is the
general-purpose fallback for targets where no good ASN is known yet.
"""

from __future__ import annotations

import os
import secrets

import httpx

from scraper_engine.core.models import AnonymityLevel, AsnClass, Proxy, ProxyProtocol


def new_session_id() -> str:
    """Returns a fresh, never-before-used DataImpulse session label.

    Numeric to match the documented examples (`sessid.45`). 18 digits of
    `secrets` entropy makes an accidental collision with a label this
    account used in the last 30 minutes (the sticky-session window)
    effectively impossible — a collision would silently hand back the
    same, already-flagged exit IP, which is the exact failure this
    function exists to prevent.
    """
    return str(secrets.randbelow(9 * 10**17) + 10**17)


def build_gateway_username(
    base_username: str,
    *,
    country: str | None = None,
    session_id: str | None = None,
    asn: int | None = None,
) -> str:
    """Appends DataImpulse targeting parameters to a gateway username.

    Returns `base_username` unchanged when neither parameter is requested,
    so the default (no country pin, gateway-chosen rotation) stays
    byte-for-byte the pre-round-62 credential.
    """
    params: list[str] = []
    if country:
        params.append(f"cr.{country.strip().lower()}")
    if asn is not None:
        params.append(f"asn.{asn}")
    if session_id:
        params.append(f"sessid.{session_id}")
    if not params:
        return base_username
    return f"{base_username}__{';'.join(params)}"


def build_gateway_proxy(
    *,
    country: str | None = None,
    session_id: str | None = None,
    asn: int | None = None,
) -> Proxy | None:
    """Constructs a Proxy for the DataImpulse gateway from env vars.

    Returns None if DATAIMPULSE_PROXY_HOST / DATAIMPULSE_PORT /
    DATAIMPULSE_USERNAME / DATAIMPULSE_PASSWORD aren't all set, or if the
    port isn't a valid integer. Callers must treat None as a hard
    misconfiguration, not a silent fallback — see Worker.__init__'s
    startup check, which calls this eagerly so a bad config fails the job
    process immediately instead of degrading every fetch silently.

    Pure function, no network I/O — safe and cheap to call on every lease
    attempt (e.g. once per _fetch_with_proxy retry), not just once at
    startup.

    Args:
        country: ISO-3166 alpha-2 exit country (config-driven tuning,
            `dataimpulse.country` in base.yaml — not a secret, so it is
            passed in rather than read from env here, keeping this
            module's documented config/secret split intact).
        session_id: sticky-session label; pass new_session_id() to force a
            different exit IP than the previous attempt got.
        asn: pin the exit to this autonomous system (`dataimpulse.asn`).
            Bare AS number, no "AS" prefix. Doubles the bandwidth bill.
    """
    host = os.environ.get("DATAIMPULSE_PROXY_HOST")
    port_raw = os.environ.get("DATAIMPULSE_PORT")
    username = os.environ.get("DATAIMPULSE_USERNAME")
    password = os.environ.get("DATAIMPULSE_PASSWORD")
    if not host or not port_raw or not username or not password:
        return None
    try:
        port = int(port_raw)
    except ValueError:
        return None
    return Proxy(
        id=-1,
        ip=host,
        port=port,
        protocol=ProxyProtocol.HTTP,
        anonymity_level=AnonymityLevel.ELITE,
        asn_class=AsnClass.RESIDENTIAL,
        reliability_score=100.0,
        username=build_gateway_username(
            username, country=country, session_id=session_id, asn=asn
        ),
        password=password,
        source="paid_gateway",
    )


# Round 66 — what proxy/dlq_reaper.py asks before re-driving a URL the
# gateway refused (FailureCategory.PROXY_AUTH_FAILED). A judge that answers
# with the caller's IP and nothing else: a few hundred bytes of plan traffic.
_PROBE_URL = "https://api.ipify.org"


async def gateway_accepts_credentials(
    *, country: str | None = None, asn: int | None = None, timeout: float = 10.0
) -> bool:
    """One real request through the gateway on a fresh session; True only on
    a 200. A 407 (plan out of traffic, bad credentials), any other status, a
    timeout or a missing configuration are all False — the caller is deciding
    whether a re-drive can succeed, and none of those say it can."""
    proxy = build_gateway_proxy(country=country, session_id=new_session_id(), asn=asn)
    if proxy is None:
        return False
    try:
        async with httpx.AsyncClient(proxy=proxy.auth_url(), timeout=timeout) as client:
            response = await client.get(_PROBE_URL)
    except httpx.HTTPError:
        return False
    return response.status_code == 200
