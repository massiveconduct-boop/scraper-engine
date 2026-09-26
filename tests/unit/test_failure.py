# tests/unit/test_failure.py
"""classify_fetch_exception: shared L1/L2/L3 exception -> FailureCategory
mapping. HOST_UNREACHABLE comes ONLY from SSRFGuard's own unresolvable-host
rejection (SSRFBlockedError.is_unresolvable) — every other exception,
including a raw DNS-failure error from the real fetch attempt, falls back to
the caller's default. See classify_fetch_exception's docstring for why."""

from datetime import UTC, datetime

import httpx
import pytest

from scraper_engine.core.exceptions import SSRFBlockedError
from scraper_engine.core.models import FailureCategory
from scraper_engine.fetcher._failure import (
    RETRY_AFTER_CAP_SECONDS,
    classify_fetch_exception,
    classify_http_status,
    parse_retry_after,
    retry_after_for,
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
    def test_401_403_404_405_410_map_to_detection_block(self):
        """Round 45 — 404 folded into the same DETECTION_BLOCK bucket as
        401/403/405/410: verified live that at least 2 of this
        deployment's own target domains return a 404-shaped response for
        an actual anti-bot block (Cloudflare), not a genuinely dead URL —
        404 alone is no more trustworthy than 403."""
        for status in (401, 403, 404, 405, 410):
            assert classify_http_status(status) == FailureCategory.DETECTION_BLOCK

    def test_429_maps_to_rate_limited(self):
        """Round 70 — "slow down", not "you're a bot": its own category."""
        assert classify_http_status(429) == FailureCategory.RATE_LIMITED

    def test_5xx_and_other_statuses_return_none(self):
        """Not specifically classified here — 5xx is already covered by
        ChallengeDetector.CHALLENGE_STATUS_CODES at the browser levels;
        callers fall back to their own default, same contract as
        classify_fetch_exception."""
        for status in (500, 502, 503, 504, 418):
            assert classify_http_status(status) is None


_NOW = datetime(2026, 9, 25, 12, 0, 0, tzinfo=UTC)


class TestParseRetryAfter:
    """Round 70 — both RFC 9110 forms, capped, garbage ignored."""

    def test_missing_header_is_none(self):
        assert parse_retry_after(None) is None

    def test_delay_seconds(self):
        assert parse_retry_after(" 120 ", now=_NOW) == 120

    def test_delay_seconds_capped(self):
        assert parse_retry_after("999999", now=_NOW) == RETRY_AFTER_CAP_SECONDS

    def test_http_date_in_the_future(self):
        assert parse_retry_after("Fri, 25 Sep 2026 12:05:00 GMT", now=_NOW) == 300

    def test_http_date_capped(self):
        assert parse_retry_after("Sat, 26 Sep 2026 12:00:00 GMT", now=_NOW) == (
            RETRY_AFTER_CAP_SECONDS
        )

    def test_http_date_in_the_past_is_zero(self):
        assert parse_retry_after("Fri, 25 Sep 2026 11:00:00 GMT", now=_NOW) == 0

    def test_naive_http_date_read_as_utc(self):
        # "-0000" makes parsedate_to_datetime return a naive datetime.
        assert parse_retry_after("Fri, 25 Sep 2026 12:01:00 -0000", now=_NOW) == 60

    def test_http_date_defaults_to_the_current_time(self):
        assert parse_retry_after("Fri, 25 Sep 2026 11:00:00 GMT") == 0

    @pytest.mark.parametrize("value", ["-5", "soon", "", "1.5"])
    def test_malformed_or_negative_is_none(self, value):
        assert parse_retry_after(value, now=_NOW) is None


class TestRetryAfterFor:
    def test_reads_the_header_of_a_429(self):
        assert retry_after_for(429, httpx.Headers({"Retry-After": "30"})) == 30

    def test_other_statuses_are_ignored(self):
        assert retry_after_for(403, httpx.Headers({"Retry-After": "30"})) is None

    def test_429_without_the_header(self):
        assert retry_after_for(429, {}) is None

    def test_non_string_value_is_ignored(self):
        assert retry_after_for(429, {"retry-after": 30}) is None

    def test_headers_without_get_are_ignored(self):
        assert retry_after_for(429, object()) is None
