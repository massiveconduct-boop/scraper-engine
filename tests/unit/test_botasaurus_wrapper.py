# tests/unit/test_botasaurus_wrapper.py
"""Round 25: Botasaurus restored from an orphaned, never-installed state
(fetcher/botasaurus_wrapper.py) and wired into Level2Fetcher as the
first-attempt path, falling back to the existing Camoufox pipeline on
failure or a detected challenge page. No real Chrome/botasaurus driver
launches here — _botasaurus_fetch (the synchronous, executor-run half) is
mocked out; these tests verify the async wiring, not botasaurus's own
browser automation."""

from unittest.mock import AsyncMock, patch

import pytest

from scraper_engine.core import budget
from scraper_engine.core.models import Proxy, ProxyProtocol
from scraper_engine.core.tenant import TenantId
from scraper_engine.fetcher.botasaurus_wrapper import BotasaurusWrapper
from scraper_engine.fetcher.level_2 import Level2Fetcher

TENANT = TenantId("botawire")
URL = "https://target.example/page"


def _proxy() -> Proxy:
    return Proxy(id=1, ip="1.2.3.4", port=8080, protocol=ProxyProtocol.HTTP)


class TestBotasaurusWrapper:
    @pytest.mark.asyncio
    async def test_fetch_html_acquires_shared_browser_semaphore(self):
        wrapper = BotasaurusWrapper()
        with patch.object(
            wrapper, "_botasaurus_fetch", return_value="<html>ok</html>"
        ) as fetch:
            assert budget.BROWSER_SEMAPHORE.locked() is False
            html = await wrapper.fetch_html(URL, proxy=_proxy(), tenant_id=TENANT)
        assert html == "<html>ok</html>"
        fetch.assert_called_once_with(URL, _proxy().url(), None)
        # Released after the call, not held open
        assert budget.BROWSER_SEMAPHORE.locked() is False

    def test_parallel_always_forced_to_one(self):
        """caller-supplied config cannot override parallel — closes F-32."""
        wrapper = BotasaurusWrapper(config={"parallel": 8, "block_images": True})
        captured: dict[str, object] = {}

        class _FakeDriver:
            page_html = "<html>x</html>"

            def get(self, url):
                pass

            def google_get(self, url, bypass_cloudflare=False):
                pass

            def short_random_sleep(self):
                pass

        def fake_browser(**kwargs):
            captured.update(kwargs)

            def decorator(fn):
                def call(*a, **kw):
                    return fn(_FakeDriver(), None)

                return call

            return decorator

        with patch("botasaurus.browser.browser", side_effect=fake_browser):
            wrapper._botasaurus_fetch(URL, "http://1.2.3.4:8080", None)

        assert captured["parallel"] == 1
        assert captured["block_images"] is True  # caller override still applies

    def _fake_browser_harness(self):
        """Shared fake @browser + Driver harness for the round-26 kwarg tests
        below — records both the decorator kwargs and the driver calls made
        inside the decorated function."""
        captured: dict[str, object] = {}
        calls: list[tuple[str, tuple]] = []

        class _FakeDriver:
            page_html = "<html>round26</html>"

            def get(self, url):
                calls.append(("get", (url,)))

            def google_get(self, url, bypass_cloudflare=False):
                calls.append(("google_get", (url, bypass_cloudflare)))

            def short_random_sleep(self):
                calls.append(("short_random_sleep", ()))

        def fake_browser(**kwargs):
            captured.update(kwargs)

            def decorator(fn):
                def call(*a, **kw):
                    return fn(_FakeDriver(), None)

                return call

            return decorator

        return captured, calls, fake_browser

    def test_bypass_cloudflare_used_by_default(self):
        wrapper = BotasaurusWrapper()
        captured, calls, fake_browser = self._fake_browser_harness()
        with patch("botasaurus.browser.browser", side_effect=fake_browser):
            wrapper._botasaurus_fetch(URL, "http://1.2.3.4:8080", None)
        assert ("google_get", (URL, True)) in calls
        assert ("get", (URL,)) not in calls
        assert ("short_random_sleep", ()) in calls

    def test_bypass_cloudflare_and_random_sleep_can_be_disabled(self):
        wrapper = BotasaurusWrapper(bypass_cloudflare=False, use_random_sleep=False)
        captured, calls, fake_browser = self._fake_browser_harness()
        with patch("botasaurus.browser.browser", side_effect=fake_browser):
            wrapper._botasaurus_fetch(URL, "http://1.2.3.4:8080", None)
        assert ("get", (URL,)) in calls
        assert ("short_random_sleep", ()) not in calls

    def test_anti_detection_kwargs_present_by_default(self):
        wrapper = BotasaurusWrapper()
        captured, _calls, fake_browser = self._fake_browser_harness()
        with patch("botasaurus.browser.browser", side_effect=fake_browser):
            wrapper._botasaurus_fetch(URL, "http://1.2.3.4:8080", None)
        # tiny_profile requires a profile (verified live against the real
        # installed botasaurus_driver: it raises ValueError("Profile must be
        # given when using tiny profile") otherwise) — must stay False here,
        # where session_id is None.
        assert captured["tiny_profile"] is False
        assert captured["remove_default_browser_check_argument"] is True
        assert captured["close_on_crash"] is True
        assert "max_retry" not in captured  # 0 (off) means omitted, not sent as 0

    def test_tiny_profile_enabled_only_when_profile_present(self):
        wrapper = BotasaurusWrapper()
        captured, _calls, fake_browser = self._fake_browser_harness()
        with patch("botasaurus.browser.browser", side_effect=fake_browser):
            wrapper._botasaurus_fetch(URL, "http://1.2.3.4:8080", "tenant:example.com")
        assert captured["tiny_profile"] is True

    def test_max_retry_only_sent_when_positive(self):
        wrapper = BotasaurusWrapper(max_retry=3)
        captured, _calls, fake_browser = self._fake_browser_harness()
        with patch("botasaurus.browser.browser", side_effect=fake_browser):
            wrapper._botasaurus_fetch(URL, "http://1.2.3.4:8080", None)
        assert captured["max_retry"] == 3

    def test_hashed_fingerprint_paired_with_profile_only(self):
        wrapper = BotasaurusWrapper()
        captured, _calls, fake_browser = self._fake_browser_harness()
        with patch("botasaurus.browser.browser", side_effect=fake_browser):
            wrapper._botasaurus_fetch(URL, "http://1.2.3.4:8080", "tenant:example.com")
        assert captured["user_agent"] == "HASHED"
        assert captured["window_size"] == "HASHED"

    def test_hashed_fingerprint_skipped_without_profile(self):
        wrapper = BotasaurusWrapper()
        captured, _calls, fake_browser = self._fake_browser_harness()
        with patch("botasaurus.browser.browser", side_effect=fake_browser):
            wrapper._botasaurus_fetch(URL, "http://1.2.3.4:8080", None)
        assert "user_agent" not in captured
        assert "window_size" not in captured


class TestLevel2BotasaurusFallback:
    @pytest.mark.asyncio
    async def test_uses_botasaurus_result_when_not_a_challenge_page(self):
        botasaurus = AsyncMock()
        botasaurus.fetch_html.return_value = "<html>real content, plenty of it</html>"
        fetcher = Level2Fetcher(botasaurus=botasaurus)

        result = await fetcher.fetch(URL, tenant_id=TENANT, proxy=_proxy())

        assert result.success is True
        assert result.level_used == 2
        assert "real content" in (result.html or "")
        botasaurus.fetch_html.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_falls_back_to_camoufox_when_botasaurus_raises(self):
        botasaurus = AsyncMock()
        botasaurus.fetch_html.side_effect = RuntimeError("driver crashed")
        fetcher = Level2Fetcher(botasaurus=botasaurus)

        with patch.object(
            fetcher, "_fetch_via_camoufox", new=AsyncMock(return_value="camoufox-result")
        ) as camoufox_fallback:
            result = await fetcher.fetch(URL, tenant_id=TENANT, proxy=_proxy())

        assert result == "camoufox-result"
        camoufox_fallback.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_falls_back_to_camoufox_on_challenge_page(self):
        botasaurus = AsyncMock()
        botasaurus.fetch_html.return_value = "<html>Checking your browser...</html>"
        fetcher = Level2Fetcher(botasaurus=botasaurus)

        with patch.object(
            fetcher, "_fetch_via_camoufox", new=AsyncMock(return_value="camoufox-result")
        ) as camoufox_fallback:
            result = await fetcher.fetch(URL, tenant_id=TENANT, proxy=_proxy())

        assert result == "camoufox-result"
        camoufox_fallback.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_skips_botasaurus_without_proxy_or_tenant(self):
        botasaurus = AsyncMock()
        fetcher = Level2Fetcher(botasaurus=botasaurus)

        with patch.object(
            fetcher, "_fetch_via_camoufox", new=AsyncMock(return_value="camoufox-result")
        ):
            await fetcher.fetch(URL, tenant_id=None, proxy=None)

        botasaurus.fetch_html.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_botasaurus_none_skips_straight_to_camoufox(self):
        fetcher = Level2Fetcher(botasaurus=None)

        with patch.object(
            fetcher, "_fetch_via_camoufox", new=AsyncMock(return_value="camoufox-result")
        ) as camoufox_fallback:
            result = await fetcher.fetch(URL, tenant_id=TENANT, proxy=_proxy())

        assert result == "camoufox-result"
        camoufox_fallback.assert_awaited_once()
