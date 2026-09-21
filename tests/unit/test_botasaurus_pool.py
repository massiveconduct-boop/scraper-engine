# tests/unit/test_botasaurus_pool.py
"""Round 26: BotasaurusPool holds a raw botasaurus Driver we construct and
key ourselves (proxy+domain match), never botasaurus's own unkeyed
reuse_driver=True pool (see browser/botasaurus_pool.py's module docstring for
why). No real Chrome/botasaurus driver launches here — botasaurus.browser.Driver
is patched out; these tests verify the reuse/eviction/close wiring, not
botasaurus's own browser automation."""

from unittest.mock import MagicMock, patch

import pytest

from scraper_engine.browser._botasaurus_nav_check import BotasaurusNavigationError
from scraper_engine.browser.botasaurus_pool import BotasaurusPool
from scraper_engine.config.schema import BotasaurusConfig
from scraper_engine.core.models import Proxy, ProxyProtocol
from scraper_engine.core.tenant import TenantId

TENANT = TenantId("botapool")


def _proxy(port: int = 8080) -> Proxy:
    return Proxy(id=1, ip="1.2.3.4", port=port, protocol=ProxyProtocol.HTTP)


def _fake_driver(html: str = "<html>fresh</html>", reuse_text: str = "<html>reused</html>"):
    driver = MagicMock()
    driver.page_html = html
    driver.requests.get.return_value = MagicMock(text=reuse_text)
    return driver


class TestBotasaurusPool:
    @pytest.mark.asyncio
    async def test_first_fetch_constructs_driver_via_google_get(self):
        pool = BotasaurusPool(tenant_id=TENANT, config=BotasaurusConfig())
        driver = _fake_driver()
        with patch("botasaurus.browser.Driver", return_value=driver) as driver_cls:
            html = await pool.fetch(
                "https://a.example/1", proxy=_proxy(), domain="a.example", session_id="s1"
            )
        driver_cls.assert_called_once()
        driver.google_get.assert_called_once_with("https://a.example/1", bypass_cloudflare=True)
        driver.requests.get.assert_not_called()
        assert html == "<html>fresh</html>"

    @pytest.mark.asyncio
    async def test_first_fetch_embeds_credentials_for_paid_gateway_proxy(self):
        """Round 40 — Driver's proxy kwarg is a single string; a paid-gateway
        Proxy's credentials must be embedded in it (auth_url()), not dropped
        (which .url() would do)."""
        gateway_proxy = Proxy(
            id=-1,
            ip="gw.dataimpulse.com",
            port=823,
            protocol=ProxyProtocol.HTTP,
            username="user123",
            password="pass456",
            source="paid_gateway",
        )
        pool = BotasaurusPool(tenant_id=TENANT, config=BotasaurusConfig())
        driver = _fake_driver()
        with patch("botasaurus.browser.Driver", return_value=driver) as driver_cls:
            await pool.fetch(
                "https://a.example/1", proxy=gateway_proxy, domain="a.example", session_id="s1"
            )
        assert driver_cls.call_args.kwargs["proxy"] == "http://user123:pass456@gw.dataimpulse.com:823"

    @pytest.mark.asyncio
    async def test_second_same_domain_fetch_reuses_driver_by_navigating(self):
        """Round 63 — reuse keeps the launched driver but now NAVIGATES it.

        It used to fire driver.requests.get(url), an in-page HTTP call with
        no JS execution, so only the first URL of a domain got a real render
        and every later one got something structurally different back. That
        made L2 success depend on input order.
        """
        pool = BotasaurusPool(tenant_id=TENANT, config=BotasaurusConfig())
        driver = _fake_driver()
        with patch("botasaurus.browser.Driver", return_value=driver) as driver_cls:
            await pool.fetch(
                "https://a.example/1", proxy=_proxy(), domain="a.example", session_id="s1"
            )
            html = await pool.fetch(
                "https://a.example/2", proxy=_proxy(), domain="a.example", session_id="s1"
            )
        driver_cls.assert_called_once()  # not called a second time
        driver.requests.get.assert_not_called()
        assert driver.google_get.call_args_list[-1].args[0] == "https://a.example/2"
        driver.close.assert_not_called()
        assert html == "<html>fresh</html>"

    @pytest.mark.asyncio
    async def test_reuse_is_keyed_on_proxy_identity_not_just_host_port(self):
        """Round 63 — Proxy.key() is ip:port, which is CONSTANT for the paid
        rotating gateway: every DataImpulse session shares one host:port and
        the username selects the exit IP. Keyed on that, a deliberately
        rotated session (round 62's fix for a blocked exit IP) hit the reuse
        branch and silently kept the blocked IP. A new session must relaunch.
        """
        pool = BotasaurusPool(tenant_id=TENANT, config=BotasaurusConfig())
        driver = _fake_driver()

        def _gateway(session: str) -> Proxy:
            return Proxy(
                id=0,
                ip="gw.dataimpulse.com",
                port=823,
                protocol=ProxyProtocol.HTTP,
                username=f"user__sessid.{session}",
                password="pw",
                source="paid_gateway",
            )

        with patch("botasaurus.browser.Driver", return_value=driver) as driver_cls:
            await pool.fetch(
                "https://a.example/1", proxy=_gateway("1"), domain="a.example", session_id="s1"
            )
            await pool.fetch(
                "https://a.example/2", proxy=_gateway("2"), domain="a.example", session_id="s1"
            )
        assert driver_cls.call_count == 2
        driver.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_network_capture_redirects_to_current_calls_sink_on_reuse(self):
        """Round 60 regression test — CDP hooks are registered once, on
        first launch, and stay live on the pooled Driver for its whole
        lifetime (tab-scoped, not per-navigation). Without redirecting to
        the *current* call's events_sink, a 2nd+ same-domain reused-driver
        fetch's captured traffic would silently land in the 1st call's
        already-returned (and unread) list instead of its own."""
        pool = BotasaurusPool(
            tenant_id=TENANT, config=BotasaurusConfig(capture_network_events=True)
        )
        driver = _fake_driver()
        with patch("botasaurus.browser.Driver", return_value=driver):
            first_sink: list[dict] = []
            await pool.fetch(
                "https://a.example/1",
                proxy=_proxy(),
                domain="a.example",
                session_id="s1",
                events_sink=first_sink,
            )
            # Registered exactly once, on the fresh-launch path.
            driver.before_request_sent.assert_called_once()
            on_request = driver.before_request_sent.call_args.args[0]

            second_sink: list[dict] = []
            await pool.fetch(
                "https://a.example/2",
                proxy=_proxy(),
                domain="a.example",
                session_id="s1",
                events_sink=second_sink,
            )
            # Still only registered once (reuse path doesn't re-register).
            driver.before_request_sent.assert_called_once()

            request = MagicMock(url="https://a.example/2", method="GET", headers={})
            on_request("req-2", request, MagicMock())

        assert second_sink == [
            {
                "type": "request",
                "request_id": "req-2",
                "url": "https://a.example/2",
                "method": "GET",
                "headers": {},
            }
        ]
        assert first_sink == []  # not the stale first call's list

    @pytest.mark.asyncio
    async def test_domain_mismatch_closes_old_driver_and_builds_new(self):
        pool = BotasaurusPool(tenant_id=TENANT, config=BotasaurusConfig())
        driver_a = _fake_driver()
        driver_b = _fake_driver(html="<html>b</html>")
        with patch("botasaurus.browser.Driver", side_effect=[driver_a, driver_b]):
            await pool.fetch(
                "https://a.example/1", proxy=_proxy(), domain="a.example", session_id="s1"
            )
            html = await pool.fetch(
                "https://b.example/1", proxy=_proxy(), domain="b.example", session_id="s1"
            )
        driver_a.close.assert_called_once()
        driver_b.google_get.assert_called_once()
        assert html == "<html>b</html>"

    @pytest.mark.asyncio
    async def test_proxy_mismatch_closes_old_driver_and_builds_new(self):
        pool = BotasaurusPool(tenant_id=TENANT, config=BotasaurusConfig())
        driver_a = _fake_driver()
        driver_b = _fake_driver(html="<html>b</html>")
        with patch("botasaurus.browser.Driver", side_effect=[driver_a, driver_b]):
            await pool.fetch(
                "https://a.example/1", proxy=_proxy(8080), domain="a.example", session_id="s1"
            )
            html = await pool.fetch(
                "https://a.example/2", proxy=_proxy(9090), domain="a.example", session_id="s1"
            )
        driver_a.close.assert_called_once()
        driver_b.google_get.assert_called_once()
        assert html == "<html>b</html>"

    @pytest.mark.asyncio
    async def test_shutdown_closes_held_driver(self):
        pool = BotasaurusPool(tenant_id=TENANT, config=BotasaurusConfig())
        driver = _fake_driver()
        with patch("botasaurus.browser.Driver", return_value=driver):
            await pool.fetch(
                "https://a.example/1", proxy=_proxy(), domain="a.example", session_id="s1"
            )
        await pool.shutdown()
        driver.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_construction_failure_closes_driver_and_propagates(self):
        pool = BotasaurusPool(tenant_id=TENANT, config=BotasaurusConfig())
        driver = _fake_driver()
        driver.google_get.side_effect = RuntimeError("nav failed")
        with (
            patch("botasaurus.browser.Driver", return_value=driver),
            pytest.raises(RuntimeError, match="nav failed"),
        ):
            await pool.fetch(
                "https://a.example/1", proxy=_proxy(), domain="a.example", session_id="s1"
            )
        driver.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_post_launch_setup_failure_closes_driver_and_propagates(self):
        """Round 60 regression test — enable_human_mode()/set_locale_and_
        timezone()/register_network_capture() run after Driver(**kwargs) but
        must stay inside the same try/except as navigation: a failure here
        (e.g. enable_human_mode()'s lazy botasaurus_humancursor import
        failing, or a CDP command throwing) must still close the driver
        instead of leaking it and its Xvfb display (round 41's
        display-contention crash precondition)."""
        pool = BotasaurusPool(
            tenant_id=TENANT, config=BotasaurusConfig(humanize_mouse=True)
        )
        driver = _fake_driver()
        driver.enable_human_mode.side_effect = RuntimeError("humancursor import failed")
        with (
            patch("botasaurus.browser.Driver", return_value=driver),
            pytest.raises(RuntimeError, match="humancursor import failed"),
        ):
            await pool.fetch(
                "https://a.example/1", proxy=_proxy(), domain="a.example", session_id="s1"
            )
        driver.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_navigation_to_chromium_error_page_raises_and_closes_driver(self):
        """Round 57 — driver.get()/google_get() never raise for a real
        network-level failure; Chromium silently renders its own
        chrome-error:// interstitial instead. This must surface as a real
        exception (and the driver must still be closed, same as any other
        construction failure), not a fake success carrying that
        interstitial as page_html."""
        pool = BotasaurusPool(tenant_id=TENANT, config=BotasaurusConfig())
        driver = _fake_driver()
        driver.current_url = "chrome-error://chromewebdata/"
        with (
            patch("botasaurus.browser.Driver", return_value=driver),
            pytest.raises(BotasaurusNavigationError) as exc_info,
        ):
            await pool.fetch(
                "https://a.example/1", proxy=_proxy(), domain="a.example", session_id="s1"
            )
        assert "https://a.example/1" in str(exc_info.value)
        driver.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_fresh_launch_autoscrolls_when_scroll_passes_configured(self):
        """Round 58 — fresh-driver fetches now scroll (lazy-load/infinite-
        scroll) when scroll_passes>0, mirroring the Camoufox pipeline."""
        pool = BotasaurusPool(tenant_id=TENANT, config=BotasaurusConfig())
        driver = _fake_driver()
        with (
            patch("botasaurus.browser.Driver", return_value=driver),
            patch(
                "scraper_engine.browser._botasaurus_scroll.botasaurus_autoscroll"
            ) as autoscroll,
        ):
            html = await pool.fetch(
                "https://a.example/1",
                proxy=_proxy(),
                domain="a.example",
                session_id="s1",
                scroll_passes=3,
                scroll_wait_ms=200,
            )
        autoscroll.assert_called_once_with(driver, max_passes=3, wait_ms=200, humanize=False)
        assert html == "<html>fresh</html>"

    @pytest.mark.asyncio
    async def test_fresh_launch_skips_autoscroll_by_default(self):
        pool = BotasaurusPool(tenant_id=TENANT, config=BotasaurusConfig())
        driver = _fake_driver()
        with (
            patch("botasaurus.browser.Driver", return_value=driver),
            patch(
                "scraper_engine.browser._botasaurus_scroll.botasaurus_autoscroll"
            ) as autoscroll,
        ):
            await pool.fetch(
                "https://a.example/1", proxy=_proxy(), domain="a.example", session_id="s1"
            )
        autoscroll.assert_not_called()

    @pytest.mark.asyncio
    async def test_reuse_fetch_autoscrolls_like_a_fresh_launch(self):
        """Round 58 skipped autoscroll on the reuse path because that path
        did not navigate — the visible DOM was the PREVIOUS page, so
        scrolling it was meaningless. Round 63 made reuse navigate, which
        removes that reason: a reused driver's DOM is now the page just
        requested, so it must lazy-load exactly like a fresh launch or the
        two paths return different content for the same URL again."""
        pool = BotasaurusPool(tenant_id=TENANT, config=BotasaurusConfig())
        driver = _fake_driver()
        with (
            patch("botasaurus.browser.Driver", return_value=driver),
            patch(
                "scraper_engine.browser._botasaurus_scroll.botasaurus_autoscroll"
            ) as autoscroll,
        ):
            await pool.fetch(
                "https://a.example/1",
                proxy=_proxy(),
                domain="a.example",
                session_id="s1",
                scroll_passes=3,
                scroll_wait_ms=200,
            )
            autoscroll.reset_mock()
            html = await pool.fetch(
                "https://a.example/2",
                proxy=_proxy(),
                domain="a.example",
                session_id="s1",
                scroll_passes=3,
                scroll_wait_ms=200,
            )
        autoscroll.assert_called_once()
        assert autoscroll.call_args.kwargs["max_passes"] == 3
        assert autoscroll.call_args.kwargs["wait_ms"] == 200
        assert html == "<html>fresh</html>"

    @pytest.mark.asyncio
    async def test_block_images_kwargs_forwarded_when_enabled(self):
        """Round 59 — real botasaurus_driver.Driver kwargs, opt-in via
        BotasaurusConfig, default False (unset by default)."""
        pool = BotasaurusPool(
            tenant_id=TENANT,
            config=BotasaurusConfig(block_images=True, block_images_and_css=True),
        )
        driver = _fake_driver()
        with patch("botasaurus.browser.Driver", return_value=driver) as driver_cls:
            await pool.fetch(
                "https://a.example/1", proxy=_proxy(), domain="a.example", session_id="s1"
            )
        assert driver_cls.call_args.kwargs["block_images"] is True
        assert driver_cls.call_args.kwargs["block_images_and_css"] is True

    @pytest.mark.asyncio
    async def test_block_images_kwargs_false_by_default(self):
        pool = BotasaurusPool(tenant_id=TENANT, config=BotasaurusConfig())
        driver = _fake_driver()
        with patch("botasaurus.browser.Driver", return_value=driver) as driver_cls:
            await pool.fetch(
                "https://a.example/1", proxy=_proxy(), domain="a.example", session_id="s1"
            )
        assert driver_cls.call_args.kwargs["block_images"] is False
        assert driver_cls.call_args.kwargs["block_images_and_css"] is False

    @pytest.mark.asyncio
    async def test_extensions_kwarg_forwarded_when_configured(self):
        """Round 60 — extensions is a real Driver kwarg, but each item must
        be an object exposing .load(with_command_line_option=False), not a
        raw path string (see browser/_botasaurus_extension.py::LocalExtension)."""
        pool = BotasaurusPool(
            tenant_id=TENANT,
            config=BotasaurusConfig(extensions=["/tmp/some-extension"]),
        )
        driver = _fake_driver()
        with patch("botasaurus.browser.Driver", return_value=driver) as driver_cls:
            await pool.fetch(
                "https://a.example/1", proxy=_proxy(), domain="a.example", session_id="s1"
            )
        forwarded = driver_cls.call_args.kwargs["extensions"]
        assert len(forwarded) == 1
        assert forwarded[0].load(with_command_line_option=False) == "/tmp/some-extension"

    @pytest.mark.asyncio
    async def test_extensions_kwarg_absent_by_default(self):
        pool = BotasaurusPool(tenant_id=TENANT, config=BotasaurusConfig())
        driver = _fake_driver()
        with patch("botasaurus.browser.Driver", return_value=driver) as driver_cls:
            await pool.fetch(
                "https://a.example/1", proxy=_proxy(), domain="a.example", session_id="s1"
            )
        assert "extensions" not in driver_cls.call_args.kwargs

    @pytest.mark.asyncio
    async def test_lang_kwarg_forwarded_when_configured(self):
        """Round 60 — Driver(lang=...) is the --lang= Chrome flag, drives
        navigator.language (driver.py:2153's own docstring note)."""
        pool = BotasaurusPool(tenant_id=TENANT, config=BotasaurusConfig(lang="en-US"))
        driver = _fake_driver()
        with patch("botasaurus.browser.Driver", return_value=driver) as driver_cls:
            await pool.fetch(
                "https://a.example/1", proxy=_proxy(), domain="a.example", session_id="s1"
            )
        assert driver_cls.call_args.kwargs["lang"] == "en-US"

    @pytest.mark.asyncio
    async def test_lang_kwarg_absent_by_default(self):
        pool = BotasaurusPool(tenant_id=TENANT, config=BotasaurusConfig())
        driver = _fake_driver()
        with patch("botasaurus.browser.Driver", return_value=driver) as driver_cls:
            await pool.fetch(
                "https://a.example/1", proxy=_proxy(), domain="a.example", session_id="s1"
            )
        assert "lang" not in driver_cls.call_args.kwargs

    @pytest.mark.asyncio
    async def test_locale_and_timezone_applied_before_navigation(self):
        """Round 60 — driver.set_locale_and_timezone() is a separate per-tab
        CDP call from Driver(lang=...), must be called before driver.get()/
        google_get() per driver.py:2148-2150's own docstring."""
        pool = BotasaurusPool(
            tenant_id=TENANT,
            config=BotasaurusConfig(locale="en_US", timezone="America/New_York"),
        )
        driver = _fake_driver()
        calls: list[str] = []
        driver.set_locale_and_timezone.side_effect = lambda **_: calls.append("locale")
        driver.google_get.side_effect = lambda *_a, **_k: calls.append("navigate")
        with patch("botasaurus.browser.Driver", return_value=driver):
            await pool.fetch(
                "https://a.example/1", proxy=_proxy(), domain="a.example", session_id="s1"
            )
        driver.set_locale_and_timezone.assert_called_once_with(
            locale="en_US", timezone_id="America/New_York"
        )
        assert calls == ["locale", "navigate"]

    @pytest.mark.asyncio
    async def test_locale_and_timezone_not_called_by_default(self):
        pool = BotasaurusPool(tenant_id=TENANT, config=BotasaurusConfig())
        driver = _fake_driver()
        with patch("botasaurus.browser.Driver", return_value=driver):
            await pool.fetch(
                "https://a.example/1", proxy=_proxy(), domain="a.example", session_id="s1"
            )
        driver.set_locale_and_timezone.assert_not_called()

    @pytest.mark.asyncio
    async def test_enable_human_mode_called_when_configured(self):
        pool = BotasaurusPool(tenant_id=TENANT, config=BotasaurusConfig(humanize_mouse=True))
        driver = _fake_driver()
        with patch("botasaurus.browser.Driver", return_value=driver):
            await pool.fetch(
                "https://a.example/1", proxy=_proxy(), domain="a.example", session_id="s1"
            )
        driver.enable_human_mode.assert_called_once()

    @pytest.mark.asyncio
    async def test_enable_human_mode_not_called_by_default(self):
        pool = BotasaurusPool(tenant_id=TENANT, config=BotasaurusConfig())
        driver = _fake_driver()
        with patch("botasaurus.browser.Driver", return_value=driver):
            await pool.fetch(
                "https://a.example/1", proxy=_proxy(), domain="a.example", session_id="s1"
            )
        driver.enable_human_mode.assert_not_called()

    @pytest.mark.asyncio
    async def test_network_capture_registered_when_configured(self):
        pool = BotasaurusPool(
            tenant_id=TENANT, config=BotasaurusConfig(capture_network_events=True)
        )
        driver = _fake_driver()
        events_sink: list[dict] = []
        with patch("botasaurus.browser.Driver", return_value=driver):
            await pool.fetch(
                "https://a.example/1",
                proxy=_proxy(),
                domain="a.example",
                session_id="s1",
                events_sink=events_sink,
            )
        driver.before_request_sent.assert_called_once()
        driver.after_response_received.assert_called_once()

    @pytest.mark.asyncio
    async def test_network_capture_not_registered_by_default(self):
        pool = BotasaurusPool(tenant_id=TENANT, config=BotasaurusConfig())
        driver = _fake_driver()
        events_sink: list[dict] = []
        with patch("botasaurus.browser.Driver", return_value=driver):
            await pool.fetch(
                "https://a.example/1",
                proxy=_proxy(),
                domain="a.example",
                session_id="s1",
                events_sink=events_sink,
            )
        driver.before_request_sent.assert_not_called()
        driver.after_response_received.assert_not_called()
