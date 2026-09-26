# fetcher/_failure.py
"""Map a raw fetch exception to a FailureCategory.

Shared by L1 (httpx) and L2/L3 (Camoufox/Playwright). HOST_UNREACHABLE comes
ONLY from SSRFGuard's own pre-flight check (SSRFBlockedError.is_unresolvable)
— see classify_fetch_exception's docstring for why a raw DNS-failure
exception caught here must NOT also map to HOST_UNREACHABLE. Everything else
falls back to the caller's default (NETWORK_TIMEOUT for L1, BROWSER_CRASH
for the browser levels) — both already retryable and already proxy-
attributable (orchestrator/worker.py's round-37 same-level fresh-proxy retry).
The one exception is a proxy refusing our credentials (PROXY_AUTH_FAILED,
round 66), which no retry fixes when that proxy is the paid gateway.
"""

from __future__ import annotations

from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

import httpx

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

    Round 66 — PROXY_AUTH_FAILED for a proxy's 407, in the two shapes seen
    live against the exhausted DataImpulse gateway: Camoufox's
    `Page.goto: NS_ERROR_PROXY_AUTHENTICATION_FAILED` and httpx's
    `ProxyError('407 TRAFFIC_EXHAUSTED')`. Both used to fall through to `default`.
    """
    if isinstance(exc, SSRFBlockedError):
        if exc.is_unresolvable:
            return FailureCategory.HOST_UNREACHABLE
        return FailureCategory.SSRF_BLOCKED
    if _is_proxy_auth_failure(exc):
        return FailureCategory.PROXY_AUTH_FAILED
    return default


def _is_proxy_auth_failure(exc: BaseException) -> bool:
    if isinstance(exc, httpx.ProxyError):
        return str(exc).startswith("407")
    return "NS_ERROR_PROXY_AUTHENTICATION_FAILED" in str(exc)


# Statuses that mean "the site actively rejected/blocked this specific
# request" — worth a real browser's chance to bypass, so these still
# escalate normally (governs L1's initial classification only; this
# function's contract is "ambiguous, don't halt", not "definitely fine").
#
# Round 45 — 404 moved INTO this set, out of a standalone permanent
# category. Live-caught: round 43 gave 404 its own NOT_FOUND category on
# the assumption a definitive "not found" status could only mean a
# genuinely dead URL — wrong. Verified live against this exact deployment's
# real target domains: nairametrics.com and techcabal.com's "404" from a
# bare/naive request was actually Cloudflare's bot-management rejection
# ("error code: 1010" — banned browser signature), and the SAME URLs the
# user confirmed load fine in a real browser. A 404 from L1 (no JS, easily
# fingerprinted) is no more trustworthy than a 403 — both need a real
# browser's chance before being believed. See
# ChallengeDetector.CHALLENGE_STATUS_CODES and orchestrator/worker.py's
# final-level confirmation check, which is where a 404 that's STILL present
# after a real browser render finally becomes a genuine, permanent
# NOT_FOUND — never from this function.
#
# Round 70 — 429 left this set for its own RATE_LIMITED: the site saying
# "slow down" is worth a later retry, a 403 usually is not. Both still
# escalate the same way (orchestrator/worker.py::_BLOCK_CATEGORIES).
_DETECTION_BLOCK_STATUSES: frozenset[int] = frozenset({401, 403, 404, 405, 410})
_RATE_LIMITED_STATUS = 429

# Round 70 — a Retry-After longer than this is treated as this long: the DLQ
# reaper still re-drives the URL, just not days later on a site's say-so.
RETRY_AFTER_CAP_SECONDS = 3600


def classify_http_status(status_code: int) -> FailureCategory | None:
    """Classify a definitively-non-2xx HTTP response status as DETECTION_BLOCK
    or RATE_LIMITED (both still worth escalating) or None (not specifically
    classified here — e.g. a bare 5xx, already covered by
    ChallengeDetector.CHALLENGE_STATUS_CODES at the browser levels; callers
    fall back to their own default, same contract as classify_fetch_exception).
    """
    if status_code in _DETECTION_BLOCK_STATUSES:
        return FailureCategory.DETECTION_BLOCK
    if status_code == _RATE_LIMITED_STATUS:
        return FailureCategory.RATE_LIMITED
    return None


def parse_retry_after(value: str | None, now: datetime | None = None) -> int | None:
    """Round 70 — a Retry-After header as whole seconds from `now`, capped at
    RETRY_AFTER_CAP_SECONDS. Accepts both RFC 9110 forms: delay-seconds
    ("120") and an HTTP-date. None for a missing, malformed or negative
    value; an HTTP-date already in the past is 0 (retry when the reaper's
    own backoff allows)."""
    if value is None:
        return None
    value = value.strip()
    if value.isdigit():
        return min(int(value), RETRY_AFTER_CAP_SECONDS)
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    delay = (when - (now or datetime.now(UTC))).total_seconds()
    return min(max(int(delay), 0), RETRY_AFTER_CAP_SECONDS)


def retry_after_for(status_code: int, headers: object) -> int | None:
    """Round 70 — the capped Retry-After of a 429 response, else None.
    `headers` is anything with a case-insensitive or lower-cased
    `.get()` (httpx.Headers, Playwright's Response.headers dict)."""
    if status_code != _RATE_LIMITED_STATUS:
        return None
    get = getattr(headers, "get", None)
    if get is None:
        return None
    raw = get("retry-after")
    return parse_retry_after(raw if isinstance(raw, str) else None)
