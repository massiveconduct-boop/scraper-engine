# proxy/asn_classifier.py
"""ASN classification for proxy scoring — closes the ASN_BONUS scoring gap.

Spec (scraper-engine-blueprint-v2.md §"Extensibility"): "swap MaxMind
GeoLite2-ASN (local DB, no external calls) in for a paid IP-reputation API
later without touching the harvester loop." ``NullAsnClassifier`` (formerly
``FakeClassifier`` in proxy/harvester.py) was the only classifier ever wired
in production — every harvested proxy landed as ``asn_class="unknown"``,
zeroing the 10% ASN_BONUS scoring dimension (proxy/scoring.py) for 100% of
proxies. ``MaxMindAsnClassifier`` is the real implementation; it activates
automatically when ``GEOIP_ASN_DB_PATH`` points at an existing GeoLite2-ASN
.mmdb file, mirroring the "gracefully inert without credentials" shape
already used for CAPTCHA providers (services/captcha_solver.py).
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from scraper_engine.proxy.harvester import SupportsClassify

logger = logging.getLogger(__name__)

# ASN organisation-name keywords for well-known hosting/cloud/CDN providers.
# GeoLite2-ASN doesn't label "datacenter vs residential" directly — this is
# the same category of heuristic MaxMind's own docs point integrators at.
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
    """Honest no-op fallback — used when no GeoLite2-ASN database is configured.

    Not a stub pretending to be real: it's the documented default when
    GEOIP_ASN_DB_PATH is unset, keeping the harvester fully functional (just
    without the ASN_BONUS scoring signal) rather than crashing.
    """

    async def classify(self, ip: str) -> str:
        return "unknown"


class MaxMindAsnClassifier:
    """Classify an IP's ASN class from a local MaxMind GeoLite2-ASN database.

    Pure local mmap lookup (no network I/O), safe to call inline from async
    code despite not being declared `async def` internally.
    """

    def __init__(self, db_path: str) -> None:
        import maxminddb

        self._reader = maxminddb.open_database(db_path)

    async def classify(self, ip: str) -> str:
        try:
            record = self._reader.get(ip)
        except (ValueError, OSError):
            return "unknown"
        if not isinstance(record, dict):
            return "unknown"

        org = str(record.get("autonomous_system_organization", "")).lower()
        if not org:
            return "unknown"
        if any(kw in org for kw in _MOBILE_KEYWORDS):
            return "mobile"
        if any(kw in org for kw in _DATACENTER_KEYWORDS):
            return "datacenter"
        return "residential"

    def close(self) -> None:
        self._reader.close()


def build_asn_classifier() -> SupportsClassify:
    """Select the ASN classifier for production use.

    Returns a MaxMindAsnClassifier when GEOIP_ASN_DB_PATH points at an
    existing file, else NullAsnClassifier — same env-gated, gracefully-inert
    pattern as services/captcha_solver.build_captcha_solver.
    """
    db_path = os.environ.get("GEOIP_ASN_DB_PATH")
    if db_path and os.path.isfile(db_path):
        try:
            return MaxMindAsnClassifier(db_path)
        except Exception:
            logger.warning("failed to open GeoLite2-ASN db at %s", db_path, exc_info=True)
    return NullAsnClassifier()
