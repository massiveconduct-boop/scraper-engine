# fetcher/_failure.py
"""Map a raw fetch exception to a FailureCategory.

Shared by L1 (httpx) and L2/L3 (Camoufox/Playwright). HOST_UNREACHABLE comes
ONLY from SSRFGuard's own pre-flight check (SSRFBlockedError.is_unresolvable)
— see classify_fetch_exception's docstring for why a raw DNS-failure
exception caught here must NOT also map to HOST_UNREACHABLE. Everything else
falls back to the caller's default (NETWORK_TIMEOUT for L1, BROWSER_CRASH
for the browser levels) — both already retryable and already proxy-
attributable (orchestrator/worker.py's round-37 same-level fresh-proxy retry).
"""

from __future__ import annotations

from scraper_engine.core.exceptions import SSRFBlockedError
from scraper_engine.core.models import FailureCategory


def classify_fetch_exception(exc: BaseException, default: FailureCategory) -> FailureCategory:
    """Return HOST_UNREACHABLE for a real SSRFGuard unresolvable-host
    rejection, SSRF_BLOCKED for a real guard block, else `default`.

    Round 43 — live-caught: a raw NS_ERROR_UNKNOWN_HOST/getaddrinfo
    exception from the ACTUAL fetch attempt used to also map to
    HOST_UNREACHABLE via string-matching, permanently blacklisting the URL.
    But every fetch path validates the URL through SSRFGuard.validate()
    BEFORE attempting the real request (level_1.py's own call; L2/L3's
    SSRFRouteGuard on every navigation/sub-request) — so by the time any
    OTHER exception reaches this function, SSRFGuard has already proven this
    exact URL resolves via a direct, unproxied lookup. A subsequent raw DNS
    exception from the real (possibly proxied) attempt can therefore only be
    proxy- or network-side — e.g. a flaky free proxy with broken DNS
    forwarding — never proof the domain itself is dead. Live-confirmed: a
    nairametrics.com URL failed once with exactly this signature while
    sibling nairametrics.com URLs succeeded in the same job; retried alone,
    it failed differently (proxy_exhausted) — never again with an unknown-
    host error, consistent with a proxy fluke, not a dead domain. Falling
    through to `default` (NETWORK_TIMEOUT/BROWSER_CRASH, both retryable and
    already wired into round 37's same-level fresh-proxy retry) is correct.

    SSRFGuard itself raises SSRFBlockedError for two different situations —
    a real block (resolved to a denied network) and an unresolvable host
    (dead domain, no DNS record at all, checked unproxied) — see
    exceptions.py::SSRFBlockedError. Only the first is actually SSRF_BLOCKED.
    """
    if isinstance(exc, SSRFBlockedError):
        if exc.is_unresolvable:
            return FailureCategory.HOST_UNREACHABLE
        return FailureCategory.SSRF_BLOCKED
    return default


# Statuses that unambiguously mean "the site actively rejected/blocked this
# specific request" rather than a network/proxy/timeout problem — worth a
# real browser render (which can bypass basic bot detection), so unlike 404
# below these still escalate normally. Kept separate from
# ChallengeDetector.CHALLENGE_STATUS_CODES (403/429/5xx) — that set governs
# whether a level-2/3 *rendered* page still looks blocked and needs another
# level; this one governs L1's initial classification, which needs the
# additional codes below (401/405/410) that a render can't help with any
# more than 403 can't, but that also aren't "the URL doesn't exist."
_DETECTION_BLOCK_STATUSES: frozenset[int] = frozenset({401, 403, 405, 410, 429})


def classify_http_status(status_code: int) -> FailureCategory | None:
    """Classify a definitively-non-2xx HTTP response status.

    Returns None for anything not specifically classified here (e.g. a
    5xx, already covered by ChallengeDetector.CHALLENGE_STATUS_CODES at the
    browser levels) — callers should fall back to their own default in that
    case, same contract as classify_fetch_exception.

    404 is the one case that gets its own category (NOT_FOUND, not
    DETECTION_BLOCK): live-caught (round 43) escalating a genuine 404
    through L2/L3 and penalizing the domain's circuit breaker over it — a
    definitively nonexistent URL will never start existing no matter which
    fetcher, proxy, or browser renders it, so unlike a real block it says
    nothing about the target's health.
    """
    if status_code == 404:
        return FailureCategory.NOT_FOUND
    if status_code in _DETECTION_BLOCK_STATUSES:
        return FailureCategory.DETECTION_BLOCK
    return None
