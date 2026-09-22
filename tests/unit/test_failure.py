# tests/unit/test_failure.py
"""classify_fetch_exception: shared L1/L2/L3 exception -> FailureCategory
mapping. HOST_UNREACHABLE comes ONLY from SSRFGuard's own unresolvable-host
rejection (SSRFBlockedError.is_unresolvable) — every other exception,
including a raw DNS-failure error from the real fetch attempt, falls back to
the caller's default. See classify_fetch_exception's docstring for why."""

import httpx
import pytest

from scraper_engine.core.exceptions import SSRFBlockedError
from scraper_engine.core.models import FailureCategory
from scraper_engine.fetcher._failure import (
    classify_fetch_exception,
    classify_http_status,
)


class TestClassifyFetchException:
    @pytest.mark.parametrize(
        "exc",
        [
            # Both verbatim from the exhausted DataImpulse gateway, round 66.
            Exception(
                "Page.goto: NS_ERROR_PROXY_AUTHENTICATION_FAILED\nCall log:\n"
                '  - navigating to "https://www.jumia.com.ng/"'
            ),
            httpx.ProxyError("407 TRAFFIC_EXHAUSTED"),
        ],
    )
    def test_proxy_refusing_credentials_maps_to_proxy_auth_failed(self, exc):
        assert (
            classify_fetch_exception(exc, FailureCategory.BROWSER_CRASH)
            == FailureCategory.PROXY_AUTH_FAILED
        )

    def test_other_proxy_errors_fall_back_to_default(self):
        exc = httpx.ProxyError("502 Bad Gateway")
        assert (
            classify_fetch_exception(exc, FailureCategory.NETWORK_TIMEOUT)
            == FailureCategory.NETWORK_TIMEOUT
        )

    def test_ssrf_blocked_error_maps_to_ssrf_blocked(self):
        exc = SSRFBlockedError(url="http://10.0.0.1/", host="10.0.0.1", network="10.0.0.0/8")
        assert (
            classify_fetch_exception(exc, FailureCategory.NETWORK_TIMEOUT)
            == FailureCategory.SSRF_BLOCKED
        )

    def test_ssrf_blocked_error_unresolvable_maps_to_host_unreachable(self):
        """SSRFGuard raises SSRFBlockedError for a dead domain too (see
        ssrf_guard.py::_resolve_hosts), not just a real block. That must
        land on HOST_UNREACHABLE — a DNS failure, not a security event."""
        exc = SSRFBlockedError(
            url="https://dead-domain.example/",
            host="dead-domain.example",
            network="<unresolvable>",
        )
        assert (
            classify_fetch_exception(exc, FailureCategory.NETWORK_TIMEOUT)
            == FailureCategory.HOST_UNREACHABLE
        )

    def test_raw_dns_failure_exception_falls_back_to_default_not_host_unreachable(self):
        """Round 43 — live-caught: every fetch path validates through
        SSRFGuard.validate() BEFORE the real request, so a raw DNS-failure
        exception reaching this function (not an SSRFBlockedError) can only
        come from the ACTUAL (possibly proxied) fetch attempt, downstream of
        SSRFGuard already proving the domain resolves unproxied. That's a
        proxy/network fluke, not proof the domain is dead — must fall
        through to the caller's retryable default, not permanently
        blacklist the URL as HOST_UNREACHABLE."""
        exc = RuntimeError("Page.goto: NS_ERROR_UNKNOWN_HOST at target.example")
        assert (
            classify_fetch_exception(exc, FailureCategory.BROWSER_CRASH)
            == FailureCategory.BROWSER_CRASH
        )
        exc2 = RuntimeError("[Errno -2] Name or service not known")
        assert (
            classify_fetch_exception(exc2, FailureCategory.NETWORK_TIMEOUT)
            == FailureCategory.NETWORK_TIMEOUT
        )

    def test_unrelated_exception_falls_back_to_default(self):
        exc = RuntimeError("connection reset by peer")
        assert (
            classify_fetch_exception(exc, FailureCategory.NETWORK_TIMEOUT)
            == FailureCategory.NETWORK_TIMEOUT
        )
        assert (
            classify_fetch_exception(exc, FailureCategory.BROWSER_CRASH)
            == FailureCategory.BROWSER_CRASH
        )


class TestClassifyHttpStatus:
    def test_401_403_404_405_410_429_map_to_detection_block(self):
        """Round 45 — 404 folded into the same DETECTION_BLOCK bucket as
        401/403/405/410/429: verified live that at least 2 of this
        deployment's own target domains return a 404-shaped response for
        an actual anti-bot block (Cloudflare), not a genuinely dead URL —
        404 alone is no more trustworthy than 403."""
        for status in (401, 403, 404, 405, 410, 429):
            assert classify_http_status(status) == FailureCategory.DETECTION_BLOCK

    def test_5xx_and_other_statuses_return_none(self):
        """Not specifically classified here — 5xx is already covered by
        ChallengeDetector.CHALLENGE_STATUS_CODES at the browser levels;
        callers fall back to their own default, same contract as
        classify_fetch_exception."""
        for status in (500, 502, 503, 504, 418):
            assert classify_http_status(status) is None
