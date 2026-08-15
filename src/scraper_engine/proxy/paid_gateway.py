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
"""

from __future__ import annotations

import os

from scraper_engine.core.models import AnonymityLevel, AsnClass, Proxy, ProxyProtocol


def build_gateway_proxy() -> Proxy | None:
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
        username=username,
        password=password,
        source="paid_gateway",
    )
