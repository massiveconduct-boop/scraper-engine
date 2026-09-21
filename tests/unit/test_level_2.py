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
    def __init__(self, html=_REAL_HTML, goto_exc=None, trigger_route_block=False, nav_status=200):
        self._html = html
        self.goto_exc = goto_exc
        self.trigger_route_block = trigger_route_block
        self._route_handler = None
        self.wait_calls = 0
        self.evaluate_calls = 0
        self.nav_status = nav_status

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
        # A real Playwright Response, not None — mirrors what page.goto()
        # actually returns on a normal http(s) navigation (round 33: the
        # production code used to discard this entirely and hardcode 200).
        return SimpleNamespace(status=self.nav_status)

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
        assert result.engine == "botasaurus"
        assert result.html == _REAL_HTML
        botasaurus_pool.fetch.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_system_exit_from_botasaurus_falls_back_to_camoufox(self):
        """Round 40 — live-caught: botasaurus_driver's proxy-auth helper
        (javascript_fixes.check_node()) calls sys.exit(1), not a normal
        raise, when Node.js isn't on PATH. SystemExit is a BaseException,
        not an Exception — must still be caught here so this module's
        documented Botasaurus->Camoufox fallback contract holds instead of
        the SystemExit propagating up and killing the whole RQ job."""
        botasaurus = MagicMock()
        botasaurus_pool = AsyncMock()
        botasaurus_pool.fetch.side_effect = SystemExit(1)
        fetcher = Level2Fetcher(botasaurus=botasaurus, botasaurus_pool=botasaurus_pool)

        result = await fetcher._fetch_via_botasaurus(
            "http://example.com", TenantId("system"), _proxy()
        )

        assert result is None  # signals "fall back to Camoufox", not a raise

    @pytest.mark.asyncio
    async def test_scroll_settings_forwarded_to_botasaurus_pool(self):
        """Round 58 — Level2Fetcher's scroll_passes/scroll_wait_ms must
        reach the pool branch, not just the Camoufox fallback pipeline."""
        botasaurus = MagicMock()
        botasaurus_pool = AsyncMock()
        botasaurus_pool.fetch.return_value = _REAL_HTML
        fetcher = Level2Fetcher(
            botasaurus=botasaurus,
            botasaurus_pool=botasaurus_pool,
            scroll_passes=4,
            scroll_wait_ms=750,
        )

        await fetcher._fetch_via_botasaurus("http://example.com", TenantId("system"), _proxy())

        botasaurus_pool.fetch.assert_awaited_once_with(
            "http://example.com",
            proxy=_proxy(),
            domain="example.com",
            session_id="system:example.com",
            scroll_passes=4,
            scroll_wait_ms=750,
            events_sink=[],
        )

    @pytest.mark.asyncio
    async def test_scroll_settings_forwarded_to_botasaurus_fetch_html(self):
        """Same as above, direct fetch_html branch (no botasaurus_pool)."""
        botasaurus = AsyncMock()
        botasaurus.fetch_html.return_value = _REAL_HTML
        fetcher = Level2Fetcher(botasaurus=botasaurus, scroll_passes=4, scroll_wait_ms=750)

        await fetcher._fetch_via_botasaurus("http://example.com", TenantId("system"), _proxy())

        botasaurus.fetch_html.assert_awaited_once_with(
            "http://example.com",
            proxy=_proxy(),
            tenant_id=TenantId("system"),
            session_id="system:example.com",
            scroll_passes=4,
            scroll_wait_ms=750,
            events_sink=[],
        )

    @pytest.mark.asyncio
    async def test_network_events_attached_when_pool_populates_sink(self):
        """Round 60 — the list passed as events_sink= gets populated in
        place by botasaurus_pool.py's own capture_network_events toggle;
        Level2Fetcher just has to read it back onto the FetchResult."""
        botasaurus = MagicMock()
        botasaurus_pool = AsyncMock()

        async def fake_fetch(*_a, events_sink=None, **_k):
            if events_sink is not None:
                events_sink.append({"type": "request", "url": "http://example.com"})
            return _REAL_HTML

        botasaurus_pool.fetch.side_effect = fake_fetch
        fetcher = Level2Fetcher(botasaurus=botasaurus, botasaurus_pool=botasaurus_pool)

        result = await fetcher._fetch_via_botasaurus(
            "http://example.com", TenantId("system"), _proxy()
        )

        assert result is not None
        assert result.network_events == [{"type": "request", "url": "http://example.com"}]

    @pytest.mark.asyncio
    async def test_network_events_none_when_sink_stays_empty(self):
        botasaurus_pool = AsyncMock()
        botasaurus_pool.fetch.return_value = _REAL_HTML
        fetcher = Level2Fetcher(botasaurus=MagicMock(), botasaurus_pool=botasaurus_pool)

        result = await fetcher._fetch_via_botasaurus(
            "http://example.com", TenantId("system"), _proxy()
        )

        assert result is not None
        assert result.network_events is None


class TestFetchViaCamoufox:
    @pytest.mark.asyncio
    async def test_cold_start_success_path(self, monkeypatch):
        page = FakePage()
        fake_wrapper_cls = MagicMock(return_value=FakeAsyncCtxMgr(FakeBrowserContext(page)))
        monkeypatch.setattr("scraper_engine.fetcher.level_2.CamoufoxWrapper", fake_wrapper_cls)
        fetcher = Level2Fetcher()

        result = await fetcher.fetch("http://example.com", TenantId("system"), proxy=_proxy())

        assert result.success is True
        assert result.html == _REAL_HTML
        fake_wrapper_cls.assert_called_once()

    @pytest.mark.asyncio
    async def test_reports_real_navigation_status_not_hardcoded_200(self, monkeypatch):
        """Round 33: FetchResult.http_status used to be hardcoded 200
        regardless of what page.goto() actually navigated to — a free
        proxy's own upstream returning 502/504 was indistinguishable from a
        real 200, which is why the gateway-error page from the original bug
        report slipped through as success. http_status must now carry the
        real navigation response status."""
        page = FakePage(nav_status=502)
        fake_wrapper_cls = MagicMock(return_value=FakeAsyncCtxMgr(FakeBrowserContext(page)))
        monkeypatch.setattr("scraper_engine.fetcher.level_2.CamoufoxWrapper", fake_wrapper_cls)
        fetcher = Level2Fetcher()

        result = await fetcher.fetch("http://example.com", TenantId("system"), proxy=_proxy())

        assert result.success is True  # unchanged — worker.py reclassifies via is_challenge_page
        assert result.http_status == 502

    @pytest.mark.asyncio
    async def test_navigation_404_reported_as_success_for_worker_to_classify(self, monkeypatch):
        """Round 45 — unlike round 43's assumption, a 404 is NOT treated as
        an immediate definitive failure here: it's now in
        ChallengeDetector.CHALLENGE_STATUS_CODES alongside 403/429/5xx, so
        worker.py's centralized is_challenge_page check decides whether to
        escalate or (at the final level) downgrade to a real failure — this
        function just reports the real status, same as the 502 case above."""
        page = FakePage(nav_status=404)
        fake_wrapper_cls = MagicMock(return_value=FakeAsyncCtxMgr(FakeBrowserContext(page)))
        monkeypatch.setattr("scraper_engine.fetcher.level_2.CamoufoxWrapper", fake_wrapper_cls)
        fetcher = Level2Fetcher()

        result = await fetcher.fetch("http://example.com", TenantId("system"), proxy=_proxy())

        assert result.success is True
        assert result.http_status == 404

    @pytest.mark.asyncio
    async def test_pool_lease_used_when_pool_configured(self):
        page = FakePage()
        pool = MagicMock()
        pool.lease.return_value = FakeAsyncCtxMgr(FakeBrowserContext(page))
        fetcher = Level2Fetcher(pool=pool)

        result = await fetcher.fetch("http://example.com", TenantId("system"), proxy=_proxy())

        assert result.success is True
        pool.lease.assert_called_once()

    @pytest.mark.asyncio
    async def test_scroll_passes_triggers_autoscroll_and_recapture(self, monkeypatch):
        page = FakePage()
        fake_wrapper_cls = MagicMock(return_value=FakeAsyncCtxMgr(FakeBrowserContext(page)))
        monkeypatch.setattr("scraper_engine.fetcher.level_2.CamoufoxWrapper", fake_wrapper_cls)
        fetcher = Level2Fetcher(scroll_passes=2, scroll_wait_ms=1)

        result = await fetcher.fetch("http://example.com", TenantId("system"), proxy=_proxy())

        assert result.success is True
        assert page.evaluate_calls > 0

    @pytest.mark.asyncio
    async def test_goto_exception_without_ssrf_block_reraises_original(self, monkeypatch):
        page = FakePage(goto_exc=TimeoutError("navigation timed out"))
        fake_wrapper_cls = MagicMock(return_value=FakeAsyncCtxMgr(FakeBrowserContext(page)))
        monkeypatch.setattr("scraper_engine.fetcher.level_2.CamoufoxWrapper", fake_wrapper_cls)
        fetcher = Level2Fetcher()

        result = await fetcher.fetch("http://example.com", TenantId("system"), proxy=_proxy())

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
        monkeypatch.setattr("scraper_engine.fetcher.level_2.CamoufoxWrapper", fake_wrapper_cls)
        fetcher = Level2Fetcher(ssrf_guard=ssrf_guard)

        result = await fetcher.fetch("http://example.com", TenantId("system"), proxy=_proxy())

        assert result.success is False
        assert "169.254.169.254" in result.error_message


class TestMaybeSolveCaptcha:
    @pytest.mark.asyncio
    async def test_returns_original_html_when_solve_fails(self, monkeypatch):
        monkeypatch.setattr(
            "scraper_engine.fetcher._captcha.solve_captcha_on_page",
            AsyncMock(return_value=False),
        )
        fetcher = Level2Fetcher(captcha_solver=MagicMock(), challenge_detector=ChallengeDetector())
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
