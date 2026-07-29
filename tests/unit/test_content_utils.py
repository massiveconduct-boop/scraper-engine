# tests/unit/test_content_utils.py
"""Shared fetch-content helpers: SSRFRouteGuard, safe_content, poll_until_solved,
autoscroll (lazy-load / infinite-scroll)."""

from unittest.mock import AsyncMock

import pytest

from scraper_engine.core.exceptions import SSRFBlockedError
from scraper_engine.fetcher._content_utils import (
    SSRFRouteGuard,
    autoscroll,
    poll_until_solved,
    safe_content,
)
from scraper_engine.fetcher.challenge_detector import ChallengeDetector


class _MockPage:
    """Minimal page stub: evaluate() returns queued heights; scrollTo is a no-op."""

    def __init__(self, heights):
        self._heights = heights
        self._i = 0
        self.waits = 0

    async def evaluate(self, js):
        if "scrollTo" in js:
            return None
        h = self._heights[min(self._i, len(self._heights) - 1)]
        self._i += 1
        return h

    async def wait_for_timeout(self, ms):
        self.waits += 1


class TestAutoscroll:
    @pytest.mark.asyncio
    async def test_stops_after_consecutive_stable(self):
        # initial=100, 200, 300, 300, 300 → grows twice, then two flat passes
        # (default stable_passes_before_stop=2) → stops on the 4th pass.
        page = _MockPage([100, 200, 300, 300, 300])
        passes = await autoscroll(page, max_passes=8, wait_ms=1)
        assert passes == 4

    @pytest.mark.asyncio
    async def test_tolerates_ajax_lag(self):
        # Regression for the round-16 bug: a single flat pass (AJAX still in
        # flight) must NOT stop the loop if the next pass grows. Heights:
        # 100 →200(grow) →200(flat, in-flight) →300(grew!) →300 →300(2 flat→stop).
        page = _MockPage([100, 200, 200, 300, 300, 300])
        passes = await autoscroll(page, max_passes=8, wait_ms=1)
        # must reach the 300 batch, i.e. not stop at the transient flat on pass 2
        assert passes == 5

    @pytest.mark.asyncio
    async def test_respects_max_passes_cap(self):
        # height grows forever → capped at max_passes (prevents infinite feed loop)
        page = _MockPage([100, 200, 300, 400, 500, 600, 700, 800, 900])
        passes = await autoscroll(page, max_passes=4, wait_ms=1)
        assert passes == 4

    @pytest.mark.asyncio
    async def test_disabled_when_zero_passes(self):
        page = _MockPage([100, 200, 300])
        assert await autoscroll(page, max_passes=0, wait_ms=1) == 0
        assert page.waits == 0  # never scrolled

    @pytest.mark.asyncio
    async def test_never_raises_on_broken_page(self):
        class Broken:
            async def evaluate(self, js):
                raise RuntimeError("page gone")

        # a page that can't be evaluated just yields 0 passes, no exception
        assert await autoscroll(Broken(), max_passes=5, wait_ms=1) == 0

    @pytest.mark.asyncio
    async def test_never_raises_on_page_broken_mid_loop(self):
        # Regression for the in-loop except/break (distinct from the pre-loop
        # guard above): the FIRST evaluate (pre-loop height read) succeeds,
        # but a later in-loop evaluate call raises — must break, not propagate.
        class BreaksAfterFirstRead:
            def __init__(self):
                self._calls = 0

            async def evaluate(self, js):
                self._calls += 1
                if self._calls == 1:
                    return 100
                raise RuntimeError("evaluate crashed mid-scroll")

            async def wait_for_timeout(self, ms):
                pass

        page = BreaksAfterFirstRead()
        assert await autoscroll(page, max_passes=5, wait_ms=1) == 0


class _RouteRequest:
    def __init__(self, url):
        self.url = url


class _MockRoute:
    def __init__(self, url):
        self.request = _RouteRequest(url)
        self.aborted_with: str | None = None
        self.continued = False

    async def abort(self, reason):
        self.aborted_with = reason

    async def continue_(self):
        self.continued = True


class TestSSRFRouteGuard:
    @pytest.mark.asyncio
    async def test_install_registers_wildcard_route(self):
        guard = SSRFRouteGuard(ssrf_guard=AsyncMock())
        page = AsyncMock()
        await guard.install(page)
        page.route.assert_awaited_once_with("**/*", guard._handle)

    @pytest.mark.asyncio
    async def test_handle_continues_when_url_allowed(self):
        ssrf_guard = AsyncMock()
        ssrf_guard.validate.return_value = None
        guard = SSRFRouteGuard(ssrf_guard=ssrf_guard)
        route = _MockRoute("https://example.com/")

        await guard._handle(route)

        assert route.continued is True
        assert route.aborted_with is None
        assert guard.blocked is None

    @pytest.mark.asyncio
    async def test_handle_aborts_and_records_blocked_url(self):
        exc = SSRFBlockedError(
            url="http://169.254.169.254/", host="169.254.169.254", network="169.254.0.0/16"
        )
        ssrf_guard = AsyncMock()
        ssrf_guard.validate.side_effect = exc
        guard = SSRFRouteGuard(ssrf_guard=ssrf_guard)
        route = _MockRoute("http://169.254.169.254/")

        await guard._handle(route)

        assert route.aborted_with == "blockedbyclient"
        assert route.continued is False
        assert guard.blocked is exc

    def test_raise_if_blocked_noop_when_nothing_blocked(self):
        guard = SSRFRouteGuard(ssrf_guard=AsyncMock())
        guard.raise_if_blocked()  # must not raise

    def test_raise_if_blocked_reraises_stored_exception(self):
        exc = SSRFBlockedError(url="http://10.0.0.1/", host="10.0.0.1", network="10.0.0.0/8")
        guard = SSRFRouteGuard(ssrf_guard=AsyncMock())
        guard.blocked = exc
        with pytest.raises(SSRFBlockedError) as excinfo:
            guard.raise_if_blocked()
        assert excinfo.value is exc


class _ContentPage:
    """Minimal page stub: content() either returns queued HTML or raises."""

    def __init__(self, contents):
        self._contents = list(contents)
        self.wait_calls = 0

    async def content(self):
        item = self._contents.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    async def wait_for_timeout(self, ms):
        self.wait_calls += 1


class TestSafeContent:
    @pytest.mark.asyncio
    async def test_returns_content_on_success(self):
        page = _ContentPage(["<html>ok</html>"])
        assert await safe_content(page) == "<html>ok</html>"

    @pytest.mark.asyncio
    async def test_returns_none_and_increments_metric_on_exception(self):
        from scraper_engine.observability.metrics import safe_content_none_total

        before = safe_content_none_total._value.get()
        page = _ContentPage([RuntimeError("mid-navigation")])
        assert await safe_content(page) is None
        assert safe_content_none_total._value.get() == before + 1


class TestPollUntilSolved:
    @pytest.mark.asyncio
    async def test_returns_immediately_when_first_read_is_real_content(self):
        detector = ChallengeDetector()
        real_html = "<html><body>" + "<p>Real article text. </p>" * 30 + "</body></html>"
        page = _ContentPage([real_html])

        result = await poll_until_solved(
            page, detector, max_total_wait_ms=1000, retry_wait_increment_ms=100
        )

        assert result == real_html
        assert page.wait_calls == 0

    @pytest.mark.asyncio
    async def test_polls_until_challenge_page_resolves(self):
        detector = ChallengeDetector()
        challenge_html = "<html><body>cf-challenge-running</body></html>"
        real_html = "<html><body>" + "<p>Real article text. </p>" * 30 + "</body></html>"
        page = _ContentPage([challenge_html, real_html])

        result = await poll_until_solved(
            page, detector, max_total_wait_ms=1000, retry_wait_increment_ms=100
        )

        assert result == real_html
        assert page.wait_calls == 1  # exactly one poll iteration before resolving

    @pytest.mark.asyncio
    async def test_gives_up_at_wait_ceiling(self):
        detector = ChallengeDetector()
        challenge_html = "<html><body>cf-challenge-running</body></html>"
        # ceiling of 200ms / 100ms increment allows exactly two polls before
        # waited_ms (200) is no longer < max_total_wait_ms (200).
        page = _ContentPage([challenge_html, challenge_html, challenge_html])

        result = await poll_until_solved(
            page, detector, max_total_wait_ms=200, retry_wait_increment_ms=100
        )

        assert result == challenge_html
        assert page.wait_calls == 2
