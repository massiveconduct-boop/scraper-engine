# tests/unit/test_level_2.py
"""Level2Fetcher — Botasaurus-first/Camoufox-fallback dispatch, the full
Camoufox pipeline (pool vs cold-start, SSRF-blocked goto, scroll), captcha
not-solved passthrough, and the raw-Playwright test seam. Was 56% covered:
nothing exercised _fetch_via_camoufox, _fetch_via_raw_playwright, or the
pool branch of _fetch_via_botasaurus."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from scraper_engine.core.exceptions import SSRFBlockedError
from scraper_engine.core.models import Proxy, ProxyProtocol
from scraper_engine.core.tenant import TenantId
from scraper_engine.fetcher.challenge_detector import ChallengeDetector
from scraper_engine.fetcher.level_2 import Level2Fetcher

_REAL_HTML = "<html><body>" + "<p>Real article text. </p>" * 30 + "</body></html>"
_CHALLENGE_HTML = "<html><body>cf-challenge-running</body></html>"


def _proxy() -> Proxy:
    return Proxy(id=1, ip="1.2.3.4", port=8080, protocol=ProxyProtocol.HTTP)


class FakePage:
    def __init__(self, html=_REAL_HTML, goto_exc=None, trigger_route_block=False):
        self._html = html
        self.goto_exc = goto_exc
        self.trigger_route_block = trigger_route_block
        self._route_handler = None
        self.wait_calls = 0
        self.evaluate_calls = 0

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


class TestInitValidation:
    def test_rejects_unknown_force_engine(self):
        with pytest.raises(ValueError, match="force_engine must be None"):
            Level2Fetcher(force_engine="chrome_devtools")


class TestFetchDispatch:
    @pytest.mark.asyncio
    async def test_force_engine_dispatches_to_raw_playwright(self, monkeypatch):
        fetcher = Level2Fetcher(force_engine="raw_playwright")
        sentinel = MagicMock()
        fetcher._fetch_via_raw_playwright = AsyncMock(return_value=sentinel)

        result = await fetcher.fetch("http://example.com", TenantId("system"), proxy=None)

        assert result is sentinel
        fetcher._fetch_via_raw_playwright.assert_awaited_once()


class TestFetchViaBotasaurus:
    @pytest.mark.asyncio
    async def test_uses_botasaurus_pool_when_configured(self):
        botasaurus = MagicMock()
        botasaurus_pool = AsyncMock()
        botasaurus_pool.fetch.return_value = _REAL_HTML
        fetcher = Level2Fetcher(botasaurus=botasaurus, botasaurus_pool=botasaurus_pool)

        result = await fetcher._fetch_via_botasaurus(
            "http://example.com", TenantId("system"), _proxy()
        )

        assert result is not None
        assert result.success is True
        assert result.html == _REAL_HTML
        botasaurus_pool.fetch.assert_awaited_once()


class TestFetchViaCamoufox:
    @pytest.mark.asyncio
    async def test_cold_start_success_path(self, monkeypatch):
        page = FakePage()
        fake_wrapper_cls = MagicMock(
            return_value=FakeAsyncCtxMgr(FakeBrowserContext(page))
        )
        monkeypatch.setattr(
            "scraper_engine.fetcher.level_2.CamoufoxWrapper", fake_wrapper_cls
        )
        fetcher = Level2Fetcher()

        result = await fetcher.fetch(
            "http://example.com", TenantId("system"), proxy=_proxy()
        )

        assert result.success is True
        assert result.html == _REAL_HTML
        fake_wrapper_cls.assert_called_once()

    @pytest.mark.asyncio
    async def test_pool_lease_used_when_pool_configured(self):
        page = FakePage()
        pool = MagicMock()
        pool.lease.return_value = FakeAsyncCtxMgr(FakeBrowserContext(page))
        fetcher = Level2Fetcher(pool=pool)

        result = await fetcher.fetch(
            "http://example.com", TenantId("system"), proxy=_proxy()
        )

        assert result.success is True
        pool.lease.assert_called_once()

    @pytest.mark.asyncio
    async def test_scroll_passes_triggers_autoscroll_and_recapture(self, monkeypatch):
        page = FakePage()
        fake_wrapper_cls = MagicMock(
            return_value=FakeAsyncCtxMgr(FakeBrowserContext(page))
        )
        monkeypatch.setattr(
            "scraper_engine.fetcher.level_2.CamoufoxWrapper", fake_wrapper_cls
        )
        fetcher = Level2Fetcher(scroll_passes=2, scroll_wait_ms=1)

        result = await fetcher.fetch(
            "http://example.com", TenantId("system"), proxy=_proxy()
        )

        assert result.success is True
        assert page.evaluate_calls > 0

    @pytest.mark.asyncio
    async def test_goto_exception_without_ssrf_block_reraises_original(self, monkeypatch):
        page = FakePage(goto_exc=TimeoutError("navigation timed out"))
        fake_wrapper_cls = MagicMock(
            return_value=FakeAsyncCtxMgr(FakeBrowserContext(page))
        )
        monkeypatch.setattr(
            "scraper_engine.fetcher.level_2.CamoufoxWrapper", fake_wrapper_cls
        )
        fetcher = Level2Fetcher()

        result = await fetcher.fetch(
            "http://example.com", TenantId("system"), proxy=_proxy()
        )

        assert result.success is False
        assert result.error_message == "navigation timed out"

    @pytest.mark.asyncio
    async def test_goto_exception_with_ssrf_block_raises_blocked_error(self, monkeypatch):
        ssrf_guard = AsyncMock()
        ssrf_guard.validate.side_effect = SSRFBlockedError(
            url="http://169.254.169.254/", host="169.254.169.254", network="169.254.0.0/16"
        )
        page = FakePage(goto_exc=RuntimeError("net::ERR_FAILED"), trigger_route_block=True)
        fake_wrapper_cls = MagicMock(
            return_value=FakeAsyncCtxMgr(FakeBrowserContext(page))
        )
        monkeypatch.setattr(
            "scraper_engine.fetcher.level_2.CamoufoxWrapper", fake_wrapper_cls
        )
        fetcher = Level2Fetcher(ssrf_guard=ssrf_guard)

        result = await fetcher.fetch(
            "http://example.com", TenantId("system"), proxy=_proxy()
        )

        assert result.success is False
        assert "169.254.169.254" in result.error_message


class TestMaybeSolveCaptcha:
    @pytest.mark.asyncio
    async def test_returns_original_html_when_solve_fails(self, monkeypatch):
        monkeypatch.setattr(
            "scraper_engine.fetcher._captcha.solve_captcha_on_page",
            AsyncMock(return_value=False),
        )
        fetcher = Level2Fetcher(
            captcha_solver=MagicMock(), challenge_detector=ChallengeDetector()
        )
        page = FakePage()

        result = await fetcher._maybe_solve_captcha(
            page, "http://example.com", TenantId("system"), _CHALLENGE_HTML
        )

        assert result == _CHALLENGE_HTML


class TestFetchViaRawPlaywright:
    @pytest.mark.asyncio
    async def test_success_path_returns_content(self, monkeypatch):
        page = AsyncMock()
        page.content.return_value = "<html>raw playwright</html>"
        context = AsyncMock()
        context.new_page.return_value = page
        browser = AsyncMock()
        browser.new_context.return_value = context
        p = SimpleNamespace(firefox=AsyncMock())
        p.firefox.launch.return_value = browser

        async_playwright_cm = AsyncMock()
        async_playwright_cm.__aenter__.return_value = p
        async_playwright_cm.__aexit__.return_value = False

        monkeypatch.setattr(
            "playwright.async_api.async_playwright",
            MagicMock(return_value=async_playwright_cm),
        )
        fetcher = Level2Fetcher(force_engine="raw_playwright")

        result = await fetcher.fetch("http://example.com", TenantId("system"), proxy=None)

        assert result.success is True
        assert result.html == "<html>raw playwright</html>"
        browser.close.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_exception_path_returns_failure(self, monkeypatch):
        monkeypatch.setattr(
            "playwright.async_api.async_playwright",
            MagicMock(side_effect=RuntimeError("playwright not installed")),
        )
        fetcher = Level2Fetcher(force_engine="raw_playwright")

        result = await fetcher.fetch("http://example.com", TenantId("system"), proxy=None)

        assert result.success is False
        assert result.error_message == "playwright not installed"
