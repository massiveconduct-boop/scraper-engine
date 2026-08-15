# fetcher/_failure.py
"""Map a raw fetch exception to a FailureCategory.

Shared by L1 (httpx) and L2/L3 (Camoufox/Playwright). The important case is
DNS / unresolvable-host: those must be HOST_UNREACHABLE (non-retryable) so a
dead domain does not waste L1→L2→L3 escalation and per-level retries. Everything
else falls back to the caller's default (NETWORK_TIMEOUT for L1, BROWSER_CRASH
for the browser levels).
"""

from __future__ import annotations

from scraper_engine.core.exceptions import SSRFBlockedError
from scraper_engine.core.models import FailureCategory

# Substrings that unambiguously indicate the host could not be resolved, across
# the two stacks: Firefox/Playwright (NS_ERROR_UNKNOWN_HOST) and libc getaddrinfo
# (httpx/asyncio).
_HOST_UNREACHABLE_MARKERS: tuple[str, ...] = (
    "NS_ERROR_UNKNOWN_HOST",
    "NS_ERROR_UNKNOWN_PROXY_HOST",
    "getaddrinfo",
    "Name or service not known",
    "nodename nor servname",
    "Temporary failure in name resolution",
    "Could not resolve host",
)


def classify_fetch_exception(exc: BaseException, default: FailureCategory) -> FailureCategory:
    """Return HOST_UNREACHABLE for DNS/unknown-host errors, SSRF_BLOCKED for a
    guard rejection (initial request or a redirect hop), else `default`.

    SSRFGuard raises SSRFBlockedError for two different situations — a real
    block (resolved to a denied network) and an unresolvable host (dead
    domain, no DNS record) — see exceptions.py::SSRFBlockedError. Only the
    first is actually SSRF_BLOCKED; the second belongs with every other
    DNS-failure path below, not lumped in with real security blocks."""
    if isinstance(exc, SSRFBlockedError):
        if exc.is_unresolvable:
            return FailureCategory.HOST_UNREACHABLE
        return FailureCategory.SSRF_BLOCKED
    msg = str(exc)
    if any(marker in msg for marker in _HOST_UNREACHABLE_MARKERS):
        return FailureCategory.HOST_UNREACHABLE
    return default
