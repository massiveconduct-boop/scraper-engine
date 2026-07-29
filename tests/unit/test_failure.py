# tests/unit/test_failure.py
"""classify_fetch_exception: shared L1/L2/L3 exception -> FailureCategory
mapping. DNS/unknown-host errors must be HOST_UNREACHABLE (non-retryable) so
a dead domain doesn't waste L1->L2->L3 escalation; SSRFBlockedError maps to
SSRF_BLOCKED; anything else falls back to the caller's default."""

import pytest

from scraper_engine.core.exceptions import SSRFBlockedError
from scraper_engine.core.models import FailureCategory
from scraper_engine.fetcher._failure import (
    _HOST_UNREACHABLE_MARKERS,
    classify_fetch_exception,
)


class TestClassifyFetchException:
    def test_ssrf_blocked_error_maps_to_ssrf_blocked(self):
        exc = SSRFBlockedError(url="http://10.0.0.1/", host="10.0.0.1", network="10.0.0.0/8")
        assert (
            classify_fetch_exception(exc, FailureCategory.NETWORK_TIMEOUT)
            == FailureCategory.SSRF_BLOCKED
        )

    @pytest.mark.parametrize("marker", _HOST_UNREACHABLE_MARKERS)
    def test_dns_markers_map_to_host_unreachable(self, marker):
        exc = RuntimeError(f"connection failed: {marker} for target.example")
        assert (
            classify_fetch_exception(exc, FailureCategory.BROWSER_CRASH)
            == FailureCategory.HOST_UNREACHABLE
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
