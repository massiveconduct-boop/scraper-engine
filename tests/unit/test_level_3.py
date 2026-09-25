# tests/unit/test_level_3.py
"""Level3Fetcher — Camoufox-only nuclear-option pipeline (pool vs cold-start,
SSRF-blocked goto, scroll, captcha not-solved passthrough). Was 54% covered:
nothing exercised fetch() at all."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from scraper_engine.core.exceptions import SSRFBlockedError
from scraper_engine.core.models import Proxy, ProxyProtocol
from scraper_engine.core.tenant import TenantId
from scraper_engine.fetcher.challenge_detector import ChallengeDetector
from scraper_engine.fetcher.level_3 import Level3Fetcher

_REAL_HTML = "<html><body>" + "<p>Real article text. </p>" * 30 + "</body></html>"
_CHALLENGE_HTML = "<html><body>cf-challenge-running</body></html>"


def _proxy() -> Proxy:
    return Proxy(id=1, ip="1.2.3.4", port=8080, protocol=ProxyProtocol.HTTP)


class FakePage:
    def __init__(
        self,
        html=_REAL_HTML,
        goto_exc=None,
        trigger_route_block=False,
        nav_status=200,
        nav_headers=None,
    ):
        self._html = html
        self.goto_exc = goto_exc
        self.trigger_route_block = trigger_route_block
        self._route_handler = None
        self.wait_calls = 0
        self.evaluate_calls = 0
        self.nav_status = nav_status
        self.nav_headers = nav_headers or {}

    async def route(self, pattern, handler):
        self._route_handler = handler

    async def goto(self, url, wait_until, timeout):
        if self.trigger_route_block and self._route_handler:
            fake_route = SimpleNamespace(
                request=SimpleNamespace(url="http://169.254.169.254/"),
                abort=AsyncMock(),
                continue_=AsyncMock(),
            )
            await self._route_handler(fake_route)
        if self.goto_exc:
            raise self.goto_exc
        # Real Playwright Response, not None (round 33 — see test_level_2.py's
        # identical fake for the full rationale).
        return SimpleNamespace(status=self.nav_status, headers=self.nav_headers)

    async def wait_for_load_state(self, state, timeout):
        return None

    async def content(self):
        return self._html

    async def wait_for_timeout(self, ms):
        self.wait_calls += 1

    async def evaluate(self, js):
        self.evaluate_calls += 1
        return 100


class FakeBrowserContext:
    def __init__(self, page):
        self._page = page

    async def new_page(self):
        return self._page


class FakeAsyncCtxMgr:
    def __init__(self, context):
        self._context = context

    async def __aenter__(self):
        return self._context

    async def __aexit__(self, exc_type, exc, tb):
        return False


class TestFetch:
    @pytest.mark.asyncio
    async def test_cold_start_success_path(self, monkeypatch):
        page = FakePage()
        fake_wrapper_cls = MagicMock(return_value=FakeAsyncCtxMgr(FakeBrowserContext(page)))
        monkeypatch.setattr("scraper_engine.fetcher.level_3.CamoufoxWrapper", fake_wrapper_cls)
        fetcher = Level3Fetcher()

        result = await fetcher.fetch("http://example.com", TenantId("system"), _proxy())

        assert result.success is True
        assert result.html == _REAL_HTML
        fake_wrapper_cls.assert_called_once()

    @pytest.mark.asyncio
    async def test_reports_real_navigation_status_not_hardcoded_200(self, monkeypatch):
        """Round 33 — same fix/rationale as Level2Fetcher's identical test:
        http_status must carry the real page.goto() response status, not a
        hardcoded 200."""
        page = FakePage(nav_status=504)
        fake_wrapper_cls = MagicMock(return_value=FakeAsyncCtxMgr(FakeBrowserContext(page)))
        monkeypatch.setattr("scraper_engine.fetcher.level_3.CamoufoxWrapper", fake_wrapper_cls)
        fetcher = Level3Fetcher()

        result = await fetcher.fetch("http://example.com", TenantId("system"), _proxy())

        assert result.success is True
        assert result.http_status == 504

    @pytest.mark.asyncio
    async def test_navigation_404_reported_as_success_for_worker_to_classify(self, monkeypatch):
        """Round 45 — same fix as Level2Fetcher's identical test: 404 is no
        longer an immediate definitive failure here — worker.py's
        centralized is_challenge_page check (404 now in
        ChallengeDetector.CHALLENGE_STATUS_CODES) is what decides whether
        the final level's own result still looks blocked and must be
        downgraded, since a browser render can't be trusted to distinguish
        a real 404 from a disguised anti-bot block on its own."""
        page = FakePage(nav_status=404)
        fake_wrapper_cls = MagicMock(return_value=FakeAsyncCtxMgr(FakeBrowserContext(page)))
        monkeypatch.setattr("scraper_engine.fetcher.level_3.CamoufoxWrapper", fake_wrapper_cls)
        fetcher = Level3Fetcher()

        result = await fetcher.fetch("http://example.com", TenantId("system"), _proxy())

        assert result.success is True
        assert result.http_status == 404

    @pytest.mark.asyncio
    @pytest.mark.parametrize(("status", "expected"), [(429, 90), (403, None), (200, None)])
    async def test_retry_after_is_read_only_from_a_429(self, monkeypatch, status, expected):
        """Round 70 — the navigation response's Retry-After becomes
        retry_after_seconds on a 429 only."""
        page = FakePage(nav_status=status, nav_headers={"retry-after": "90"})
        fake_wrapper_cls = MagicMock(return_value=FakeAsyncCtxMgr(FakeBrowserContext(page)))
        monkeypatch.setattr("scraper_engine.fetcher.level_3.CamoufoxWrapper", fake_wrapper_cls)

        result = await Level3Fetcher().fetch("http://example.com", TenantId("system"), _proxy())

        assert result.http_status == status
        assert result.retry_after_seconds == expected

    @pytest.mark.asyncio
    async def test_pool_lease_used_when_pool_configured(self):
        page = FakePage()
        pool = MagicMock()
        pool.lease.return_value = FakeAsyncCtxMgr(FakeBrowserContext(page))
        fetcher = Level3Fetcher(pool=pool)

        result = await fetcher.fetch("http://example.com", TenantId("system"), _proxy())

        assert result.success is True
        pool.lease.assert_called_once()

    @pytest.mark.asyncio
    async def test_scroll_passes_triggers_autoscroll_and_recapture(self, monkeypatch):
        page = FakePage()
        fake_wrapper_cls = MagicMock(return_value=FakeAsyncCtxMgr(FakeBrowserContext(page)))
        monkeypatch.setattr("scraper_engine.fetcher.level_3.CamoufoxWrapper", fake_wrapper_cls)
        fetcher = Level3Fetcher(scroll_passes=2, scroll_wait_ms=1)

        result = await fetcher.fetch("http://example.com", TenantId("system"), _proxy())

        assert result.success is True
        assert page.evaluate_calls > 0

    @pytest.mark.asyncio
    async def test_goto_exception_without_ssrf_block_reraises_original(self, monkeypatch):
        page = FakePage(goto_exc=TimeoutError("navigation timed out"))
        fake_wrapper_cls = MagicMock(return_value=FakeAsyncCtxMgr(FakeBrowserContext(page)))
        monkeypatch.setattr("scraper_engine.fetcher.level_3.CamoufoxWrapper", fake_wrapper_cls)
        fetcher = Level3Fetcher()

        result = await fetcher.fetch("http://example.com", TenantId("system"), _proxy())

        assert result.success is False
        assert result.error_message == "navigation timed out"

    @pytest.mark.asyncio
    async def test_goto_exception_with_ssrf_block_raises_blocked_error(self, monkeypatch):
        ssrf_guard = AsyncMock()
        ssrf_guard.validate.side_effect = SSRFBlockedError(
            url="http://169.254.169.254/", host="169.254.169.254", network="169.254.0.0/16"
        )
        page = FakePage(goto_exc=RuntimeError("net::ERR_FAILED"), trigger_route_block=True)
        fake_wrapper_cls = MagicMock(return_value=FakeAsyncCtxMgr(FakeBrowserContext(page)))
        monkeypatch.setattr("scraper_engine.fetcher.level_3.CamoufoxWrapper", fake_wrapper_cls)
        fetcher = Level3Fetcher(ssrf_guard=ssrf_guard)

        result = await fetcher.fetch("http://example.com", TenantId("system"), _proxy())

        assert result.success is False
        assert "169.254.169.254" in result.error_message


class TestMaybeSolveCaptcha:
    @pytest.mark.asyncio
    async def test_returns_original_html_when_solve_fails(self, monkeypatch):
        monkeypatch.setattr(
            "scraper_engine.fetcher._captcha.solve_captcha_on_page",
            AsyncMock(return_value=False),
        )
        fetcher = Level3Fetcher(captcha_solver=MagicMock(), challenge_detector=ChallengeDetector())
        page = FakePage()

        result = await fetcher._maybe_solve_captcha(
            page, "http://example.com", TenantId("system"), _CHALLENGE_HTML
        )

        assert result == _CHALLENGE_HTML


class TestPostLoadFixedWait:
    """Round 63 — post_load_fixed_wait_ms is paid only by pages that look
    like a challenge.

    It used to run unconditionally, before anything had looked at the page,
    so every L3 fetch spent 10s (the live value) whether or not there was a
    proof-of-work solver to wait for. A domain that escalates to L3 tends to
    stay there for a whole crawl, so that was 10s times every URL of the job
    for the majority of pages that render fine once a real browser asks.
    """

    @pytest.mark.asyncio
    async def test_clean_page_skips_the_fixed_wait(self, monkeypatch):
        page = FakePage()
        monkeypatch.setattr(
            "scraper_engine.fetcher.level_3.CamoufoxWrapper",
            MagicMock(return_value=FakeAsyncCtxMgr(FakeBrowserContext(page))),
        )
        fetcher = Level3Fetcher(post_load_fixed_wait_ms=10000, scroll_passes=0)

        result = await fetcher.fetch("http://example.com", TenantId("system"), _proxy())

        assert result.success is True
        assert result.html == _REAL_HTML
        assert page.wait_calls == 0

    @pytest.mark.asyncio
    async def test_challenge_page_still_pays_the_full_budget(self, monkeypatch):
        page = FakePage(html=_CHALLENGE_HTML)
        monkeypatch.setattr(
            "scraper_engine.fetcher.level_3.CamoufoxWrapper",
            MagicMock(return_value=FakeAsyncCtxMgr(FakeBrowserContext(page))),
        )
        fetcher = Level3Fetcher(
            post_load_fixed_wait_ms=10000,
            max_total_wait_ms=30000,
            retry_wait_increment_ms=5000,
            scroll_passes=0,
            challenge_detector=ChallengeDetector(),
        )

        await fetcher.fetch("http://example.com", TenantId("system"), _proxy())

        # One fixed wait, then the poll loop's 5s increments to the 30s ceiling.
        assert page.wait_calls == 5

    @pytest.mark.asyncio
    async def test_blocking_nav_status_counts_as_needing_settling(self, monkeypatch):
        """A 403 body can read as ordinary content; the status is what makes
        it a challenge. Passing the real nav status into the first check (not
        a hardcoded 200) is what keeps those pages on the settling path."""
        page = FakePage(nav_status=403)
        monkeypatch.setattr(
            "scraper_engine.fetcher.level_3.CamoufoxWrapper",
            MagicMock(return_value=FakeAsyncCtxMgr(FakeBrowserContext(page))),
        )
        fetcher = Level3Fetcher(
            post_load_fixed_wait_ms=10000, scroll_passes=0, challenge_detector=ChallengeDetector()
        )

        await fetcher.fetch("http://example.com", TenantId("system"), _proxy())

        assert page.wait_calls > 0
