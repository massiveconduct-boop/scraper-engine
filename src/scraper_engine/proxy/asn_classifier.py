# proxy/asn_classifier.py
"""ASN classification for proxy scoring — closes the ASN_BONUS scoring gap.

``NullAsnClassifier`` (formerly ``FakeClassifier`` in proxy/harvester.py)
was the only classifier ever wired in production — every harvested proxy
landed as ``asn_class="unknown"``, zeroing the 10% ASN_BONUS scoring
dimension (proxy/scoring.py) for 100% of proxies.

``ReverseDnsAsnClassifier`` is the real implementation: it asks DNS "what
hostname points at this IP" (a PTR lookup) and matches the same
hosting/mobile keyword lists a MaxMind GeoLite2-ASN org-name lookup would
have used, but against that hostname instead. No third-party account, no
license key, no database file to download and keep refreshing — just a
standard DNS lookup already available wherever this process has network
access. Less precise than a maintained IP-to-ASN database (some datacenters
skip a descriptive PTR, some residential ISPs set one), but zero external
dependency, so it's wired in unconditionally rather than gated behind an
env var.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from scraper_engine.proxy.harvester import SupportsClassify

# Hostname keywords for well-known hosting/cloud/CDN providers — matched
# against a PTR record's hostname (e.g. "ec2-1-2-3-4.compute-1.amazonaws.com").
_DATACENTER_KEYWORDS = (
    "amazon",
    "aws",
    "google",
    "microsoft",
    "azure",
    "digitalocean",
    "linode",
    "vultr",
    "ovh",
    "hetzner",
    "oracle",
    "cloudflare",
    "akamai",
    "fastly",
    "alibaba",
    "tencent",
    "scaleway",
    "contabo",
    "leaseweb",
    "hosting",
    "datacenter",
    "data center",
    "colocation",
    "server",
)
_MOBILE_KEYWORDS = (
    "mobile",
    "wireless",
    "cellular",
    "t-mobile",
    "verizon wireless",
    "vodafone",
    "airtel",
    "jio",
    "cellco",
)


class NullAsnClassifier:
    """Honest no-op fallback — used when ASN classification is disabled.

    Not a stub pretending to be real: it's the documented default for
    callers (e.g. tests) that construct a harvester without an explicit
    classifier, keeping the harvester fully functional (just without the
    ASN_BONUS scoring signal) rather than crashing.
    """

    async def classify(self, ip: str) -> str:
        return "unknown"


class ReverseDnsAsnClassifier:
    """Classify an IP's ASN class via reverse-DNS (PTR) hostname lookup.

    No external account, no downloaded database, no license key to
    maintain — a standard DNS PTR lookup, matched against the same keyword
    lists a MaxMind org-name lookup would have used. Less precise (some
    datacenters skip a descriptive PTR, some residential ISPs set one) but
    zero third-party dependency.
    """

    def __init__(self, timeout_seconds: float = 2.0) -> None:
        self._timeout_seconds = timeout_seconds

    async def classify(self, ip: str) -> str:
        loop = asyncio.get_running_loop()
        try:
            hostname, _ = await asyncio.wait_for(
                loop.getnameinfo((ip, 0), 0), timeout=self._timeout_seconds
            )
        except (OSError, TimeoutError):
            return "unknown"
        hostname = hostname.lower()
        if hostname == ip:
            # No PTR record — getnameinfo() echoed the IP back as a string.
            return "unknown"
        if any(kw in hostname for kw in _MOBILE_KEYWORDS):
            return "mobile"
        if any(kw in hostname for kw in _DATACENTER_KEYWORDS):
            return "datacenter"
        return "residential"


def build_asn_classifier() -> SupportsClassify:
    """Select the ASN classifier for production use.

    Always returns a ReverseDnsAsnClassifier — no credential or database
    file is needed, so unlike services/captcha_solver.build_captcha_solver
    there's no "inert until configured" branch here.
    """
    return ReverseDnsAsnClassifier()
