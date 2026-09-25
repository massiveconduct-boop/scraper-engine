# tests/unit/test_level_1.py
"""Level1Fetcher — plain-httpx redirect chain and timeout/exception
handling. Complements test_level1_ja3_wiring.py, which covers the
JA3-first/httpx-fallback wiring but not these branches. Markdown conversion
moved to Worker.process_job in round 29 — see test_worker.py."""

from unittest.mock import AsyncMock

import httpx
import pytest

from scraper_engine.core.models import FailureCategory
from scraper_engine.core.tenant import TenantId
from scraper_engine.fetcher.level_1 import Level1Fetcher
from scraper_engine.fetcher.scrapling_wrapper import ScraplingResponse


class _FakeResponse:
    def __init__(self, status_code, text="<html>ok</html>", location=None, is_redirect=False):
        self.status_code = status_code
        self.text = text
        self.is_redirect = is_redirect
        self.url = "http://example.com/"
        self.headers = {"location": location} if location else {}


class _RedirectThenFinalClient:
    def __init__(self, *a, **kw):
        self._calls = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url):
        self._calls += 1
        if self._calls == 1:
            return _FakeResponse(302, text="", location="/next", is_redirect=True)
        return _FakeResponse(200, text="<html>final</html>")


class _TimeoutClient:
    def __init__(self, *a, **kw): ...
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url):
        raise httpx.TimeoutException("timed out")


class _BoomClient:
    def __init__(self, *a, **kw): ...
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url):
        raise ValueError("unexpected boom")


class TestPlainHttpxRedirects:
    @pytest.mark.asyncio
    async def test_follows_one_redirect_and_revalidates_ssrf(self, monkeypatch):
        monkeypatch.setattr(httpx, "AsyncClient", _RedirectThenFinalClient)
        fetcher = Level1Fetcher()

        result = await fetcher.fetch("http://example.com", TenantId("system"))

        assert result.success is True
        assert result.html == "<html>final</html>"
        assert result.http_status == 200


class _StatusClient:
    def __init__(self, status_code):
        self._status_code = status_code

    def __call__(self, *a, **kw):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url):
        return _FakeResponse(self._status_code, text="<html>error page</html>")


class TestPlainHttpxStatusClassification:
    """Round 43 — a plain HTTP failure status now carries a real
    failure_category instead of falling through untagged."""

    @pytest.mark.asyncio
    async def test_404_classified_as_detection_block(self, monkeypatch):
        """Round 45 — a 404 from L1 (no JS, easily fingerprinted) is treated
        the same as any other block status: worth a real browser's chance,
        not an immediate, definitive failure. Live-caught: this exact
        deployment's own target domains returned a 404-shaped response for
        what was actually a Cloudflare bot-management block."""
        monkeypatch.setattr(httpx, "AsyncClient", _StatusClient(404))
        fetcher = Level1Fetcher()

        result = await fetcher.fetch("http://example.com", TenantId("system"))

        assert result.success is False
        assert result.failure_category == FailureCategory.DETECTION_BLOCK

    @pytest.mark.asyncio
    async def test_403_classified_as_detection_block(self, monkeypatch):
        monkeypatch.setattr(httpx, "AsyncClient", _StatusClient(403))
        fetcher = Level1Fetcher()

        result = await fetcher.fetch("http://example.com", TenantId("system"))

        assert result.success is False
        assert result.failure_category == FailureCategory.DETECTION_BLOCK

    @pytest.mark.asyncio
    async def test_5xx_left_uncategorized(self, monkeypatch):
        """5xx isn't classified here — ChallengeDetector.CHALLENGE_STATUS_CODES
        already handles it downstream in worker.py's escalation logic."""
        monkeypatch.setattr(httpx, "AsyncClient", _StatusClient(503))
        fetcher = Level1Fetcher()

        result = await fetcher.fetch("http://example.com", TenantId("system"))

        assert result.success is False
        assert result.failure_category is None


class _HeadersStatusClient(_StatusClient):
    """A _StatusClient whose response carries real httpx.Headers."""

    def __init__(self, status_code, headers):
        super().__init__(status_code)
        self._headers = headers

    async def get(self, url):
        response = _FakeResponse(self._status_code, text="<html>slow down</html>")
        response.headers = httpx.Headers(self._headers)
        return response


class TestRetryAfter:
    """Round 70 — a 429 is RATE_LIMITED and carries the site's Retry-After
    (fetcher/_failure.py::retry_after_for); any other status leaves it None."""

    @pytest.mark.asyncio
    async def test_httpx_429_reads_retry_after(self, monkeypatch):
        monkeypatch.setattr(httpx, "AsyncClient", _HeadersStatusClient(429, {"Retry-After": "120"}))

        result = await Level1Fetcher().fetch("http://example.com", TenantId("system"))

        assert result.success is False
        assert result.failure_category == FailureCategory.RATE_LIMITED
        assert result.retry_after_seconds == 120

    @pytest.mark.asyncio
    async def test_httpx_non_429_ignores_retry_after(self, monkeypatch):
        monkeypatch.setattr(httpx, "AsyncClient", _HeadersStatusClient(503, {"Retry-After": "120"}))

        result = await Level1Fetcher().fetch("http://example.com", TenantId("system"))

        assert result.retry_after_seconds is None

    @pytest.mark.asyncio
    async def test_scrapling_429_reads_retry_after(self):
        scrapling = AsyncMock()
        scrapling.fetch.return_value = ScraplingResponse(
            status_code=429, text="<html>slow down</html>", location=None, retry_after="30"
        )

        result = await Level1Fetcher(scrapling_client=scrapling).fetch(
            "http://example.com", TenantId("system")
        )

        assert result.failure_category == FailureCategory.RATE_LIMITED
        assert result.retry_after_seconds == 30

    @pytest.mark.asyncio
    async def test_scrapling_non_429_ignores_retry_after(self):
        scrapling = AsyncMock()
        scrapling.fetch.return_value = ScraplingResponse(
            status_code=403, text="<html>no</html>", location=None, retry_after="30"
        )

        result = await Level1Fetcher(scrapling_client=scrapling).fetch(
            "http://example.com", TenantId("system")
        )

        assert result.failure_category == FailureCategory.DETECTION_BLOCK
        assert result.retry_after_seconds is None


class TestPlainHttpxExceptions:
    @pytest.mark.asyncio
    async def test_timeout_returns_network_timeout_failure(self, monkeypatch):
        monkeypatch.setattr(httpx, "AsyncClient", _TimeoutClient)
        fetcher = Level1Fetcher()

        result = await fetcher.fetch("http://example.com", TenantId("system"))

        assert result.success is False
        assert result.failure_category == FailureCategory.NETWORK_TIMEOUT
        assert result.error_message == "Request timed out"

    @pytest.mark.asyncio
    async def test_generic_exception_classified_and_captured(self, monkeypatch):
        monkeypatch.setattr(httpx, "AsyncClient", _BoomClient)
        fetcher = Level1Fetcher()

        result = await fetcher.fetch("http://example.com", TenantId("system"))

        assert result.success is False
        assert result.error_message == "unexpected boom"


class TestScraplingWiring:
    @pytest.mark.asyncio
    async def test_uses_scrapling_result_when_it_succeeds(self):
        scrapling = AsyncMock()
        scrapling.fetch.return_value = ScraplingResponse(
            status_code=200, text="<html>scrapling</html>", location=None
        )
        fetcher = Level1Fetcher(scrapling_client=scrapling)

        result = await fetcher.fetch("http://example.com", TenantId("system"))

        assert result.success is True
        assert result.html == "<html>scrapling</html>"
        scrapling.fetch.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_scrapling_404_classified_as_detection_block(self):
        """Round 45 — the scrapling path's own status-based classification,
        same fix as plain httpx's in TestPlainHttpxStatusClassification."""
        scrapling = AsyncMock()
        scrapling.fetch.return_value = ScraplingResponse(
            status_code=404, text="<html>gone</html>", location=None
        )
        fetcher = Level1Fetcher(scrapling_client=scrapling)

        result = await fetcher.fetch("http://example.com", TenantId("system"))

        assert result.success is False
        assert result.failure_category == FailureCategory.DETECTION_BLOCK

    @pytest.mark.asyncio
    async def test_follows_redirect_and_revalidates_ssrf(self):
        scrapling = AsyncMock()
        scrapling.fetch.side_effect = [
            ScraplingResponse(status_code=302, text="", location="/next"),
            ScraplingResponse(status_code=200, text="<html>final</html>", location=None),
        ]
        fetcher = Level1Fetcher(scrapling_client=scrapling)

        result = await fetcher.fetch("http://example.com", TenantId("system"))

        assert result.success is True
        assert result.html == "<html>final</html>"
        assert scrapling.fetch.await_count == 2

    @pytest.mark.asyncio
    async def test_redirect_to_blocked_target_falls_through_to_httpx_failure(self, monkeypatch):
        """SSRF block during the scrapling redirect loop is swallowed by
        _fetch_via_scrapling's fallback contract (returns None, same as
        _fetch_via_ja3) — fetch() then retries via plain httpx against the
        original URL, which hits the identical blocked hop and surfaces a
        real failure instead of silently succeeding."""
        scrapling = AsyncMock()
        scrapling.fetch.return_value = ScraplingResponse(
            status_code=302, text="", location="http://169.254.169.254/"
        )
        monkeypatch.setattr(httpx, "AsyncClient", _RedirectThenFinalClient)
        fetcher = Level1Fetcher(scrapling_client=scrapling)

        result = await fetcher.fetch("http://example.com", TenantId("system"))

        # httpx fallback path (_RedirectThenFinalClient) succeeds since it
        # redirects to a harmless "/next", not the blocked scrapling target —
        # proves control genuinely passed to the next engine in the chain.
        assert result.success is True
        assert result.html == "<html>final</html>"

    @pytest.mark.asyncio
    async def test_falls_back_when_post_redirect_scrapling_fetch_returns_none(self, monkeypatch):
        scrapling = AsyncMock()
        scrapling.fetch.side_effect = [
            ScraplingResponse(status_code=302, text="", location="/next"),
            None,
        ]
        monkeypatch.setattr(httpx, "AsyncClient", _RedirectThenFinalClient)
        fetcher = Level1Fetcher(scrapling_client=scrapling)

        result = await fetcher.fetch("http://example.com", TenantId("system"))

        assert result.success is True
        assert result.html == "<html>final</html>"
        assert scrapling.fetch.await_count == 2

    @pytest.mark.asyncio
    async def test_falls_back_to_httpx_when_scrapling_returns_none(self, monkeypatch):
        scrapling = AsyncMock()
        scrapling.fetch.return_value = None
        monkeypatch.setattr(httpx, "AsyncClient", _RedirectThenFinalClient)
        fetcher = Level1Fetcher(scrapling_client=scrapling)

        result = await fetcher.fetch("http://example.com", TenantId("system"))

        assert result.success is True
        assert result.html == "<html>final</html>"


class _EndlessRedirectClient:
    def __init__(self, *a, **kw):
        self.calls = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url):
        self.calls += 1
        return _FakeResponse(302, text="<html>moved</html>", location="/again", is_redirect=True)


class TestRedirectLimit:
    """Round 64 — each engine's redirect loop fell out of
    `for _ in range(MAX_REDIRECTS)` with a 3xx still in hand and then computed
    `success = status < 400`, so an endless redirect was a SUCCESS whose
    content was the redirect body. It must be a DETECTION_BLOCK (escalates to
    a real browser) instead."""

    def _assert_redirect_limit(self, result):
        assert result.success is False
        assert result.failure_category == FailureCategory.DETECTION_BLOCK
        assert result.http_status == 302
        assert "Redirect limit" in (result.error_message or "")

    @pytest.mark.asyncio
    async def test_httpx_endless_redirect_is_a_failure(self, monkeypatch):
        monkeypatch.setattr(httpx, "AsyncClient", _EndlessRedirectClient)
        result = await Level1Fetcher().fetch("http://example.com", TenantId("system"))
        self._assert_redirect_limit(result)

    @pytest.mark.asyncio
    async def test_ja3_endless_redirect_is_a_failure(self):
        response = AsyncMock()
        response.status_code = 302
        response.location = "/again"
        response.text = "<html>moved</html>"
        session = AsyncMock()
        session.get.return_value = response
        ja3 = AsyncMock()
        ja3.open_session.return_value = session
        result = await Level1Fetcher(ja3_client=ja3).fetch("http://example.com", TenantId("system"))
        self._assert_redirect_limit(result)

    @pytest.mark.asyncio
    async def test_scrapling_endless_redirect_is_a_failure(self):
        scrapling = AsyncMock()
        scrapling.fetch.return_value = ScraplingResponse(
            status_code=302, text="<html>moved</html>", location="/again"
        )
        result = await Level1Fetcher(scrapling_client=scrapling).fetch(
            "http://example.com", TenantId("system")
        )
        self._assert_redirect_limit(result)

    @pytest.mark.asyncio
    async def test_a_chain_that_ends_inside_the_limit_still_succeeds(self, monkeypatch):
        monkeypatch.setattr(httpx, "AsyncClient", _RedirectThenFinalClient)
        result = await Level1Fetcher().fetch("http://example.com", TenantId("system"))
        assert result.success is True
