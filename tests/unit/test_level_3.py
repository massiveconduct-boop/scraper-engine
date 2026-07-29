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
