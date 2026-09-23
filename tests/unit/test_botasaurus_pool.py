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
        assert (
            driver_cls.call_args.kwargs["proxy"] == "http://user123:pass456@gw.dataimpulse.com:823"
        )

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
        pool = BotasaurusPool(tenant_id=TENANT, config=BotasaurusConfig(max_pooled_drivers=1))
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
        # Round 67 — and neither is parked: a gateway session is single-use
        # (Proxy.reusable), so each driver closes after its own fetch.
        assert driver.close.call_count == 2
        assert pool._entries == []

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
        pool = BotasaurusPool(tenant_id=TENANT, config=BotasaurusConfig(max_pooled_drivers=1))
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
        pool = BotasaurusPool(tenant_id=TENANT, config=BotasaurusConfig(max_pooled_drivers=1))
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
        pool = BotasaurusPool(tenant_id=TENANT, config=BotasaurusConfig(humanize_mouse=True))
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
            patch("scraper_engine.browser._botasaurus_scroll.botasaurus_autoscroll") as autoscroll,
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
            patch("scraper_engine.browser._botasaurus_scroll.botasaurus_autoscroll") as autoscroll,
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
            patch("scraper_engine.browser._botasaurus_scroll.botasaurus_autoscroll") as autoscroll,
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


class TestMultiDriverPool:
    """Round 64 — up to max_pooled_drivers drivers per job; the display lock
    covers launch/close only; a fetch holds a browser permit only while it
    runs. Live motivation: five concurrent L2 URLs took 43/65/89/58/134s
    because they queued single-file behind one driver, and every relaunch
    held XVFB_LOCK through its whole navigation, stalling Camoufox too."""

    @pytest.fixture(autouse=True)
    def _budget(self, monkeypatch):
        import asyncio as _asyncio

        from scraper_engine.core import budget

        monkeypatch.setattr(budget, "BROWSER_SEMAPHORE", _asyncio.Semaphore(4))
        monkeypatch.setattr(budget, "XVFB_LOCK", _asyncio.Lock())
        monkeypatch.setattr(budget, "_reclaimers", [])
        return budget

    @pytest.mark.asyncio
    async def test_a_second_pair_gets_its_own_driver_and_keeps_the_first(self):
        pool = BotasaurusPool(tenant_id=TENANT, config=BotasaurusConfig(max_pooled_drivers=2))
        a, b = _fake_driver(), _fake_driver(html="<html>b</html>")
        with patch("botasaurus.browser.Driver", side_effect=[a, b]):
            await pool.fetch(
                "https://a.example/1", proxy=_proxy(), domain="a.example", session_id="s1"
            )
            await pool.fetch(
                "https://b.example/1", proxy=_proxy(), domain="b.example", session_id="s1"
            )
            await pool.fetch(
                "https://a.example/2", proxy=_proxy(), domain="a.example", session_id="s1"
            )
        a.close.assert_not_called()
        b.close.assert_not_called()
        assert a.google_get.call_count == 2

    @pytest.mark.asyncio
    async def test_over_the_cap_the_oldest_idle_driver_is_closed(self):
        pool = BotasaurusPool(tenant_id=TENANT, config=BotasaurusConfig(max_pooled_drivers=2))
        a, b, c = _fake_driver(), _fake_driver(), _fake_driver()
        with patch("botasaurus.browser.Driver", side_effect=[a, b, c]):
            for d in ("a", "b", "c"):
                await pool.fetch(
                    f"https://{d}.example/", proxy=_proxy(), domain=f"{d}.example", session_id="s1"
                )
        a.close.assert_called_once()
        b.close.assert_not_called()
        assert len(pool._entries) == 2

    @pytest.mark.asyncio
    async def test_concurrent_fetches_run_in_parallel(self):
        import asyncio as _asyncio
        import threading

        pool = BotasaurusPool(tenant_id=TENANT, config=BotasaurusConfig(max_pooled_drivers=2))
        both_navigating = threading.Barrier(2, timeout=2)

        def _driver():
            d = _fake_driver()
            d.google_get.side_effect = lambda *a, **k: both_navigating.wait()
            return d

        with patch("botasaurus.browser.Driver", side_effect=[_driver(), _driver()]):
            await _asyncio.wait_for(
                _asyncio.gather(
                    pool.fetch(
                        "https://a.example/", proxy=_proxy(), domain="a.example", session_id="s1"
                    ),
                    pool.fetch(
                        "https://b.example/", proxy=_proxy(), domain="b.example", session_id="s1"
                    ),
                ),
                timeout=5,
            )

    @pytest.mark.asyncio
    async def test_navigation_runs_outside_the_display_lock_but_inside_a_permit(self, _budget):
        budget = _budget
        pool = BotasaurusPool(tenant_id=TENANT, config=BotasaurusConfig())
        seen: dict[str, object] = {}
        driver = _fake_driver()

        def _nav(*_a, **_k):
            seen["xvfb_locked"] = budget.XVFB_LOCK.locked()
            seen["permits_left"] = budget.BROWSER_SEMAPHORE._value

        driver.google_get.side_effect = _nav
        with patch("botasaurus.browser.Driver", return_value=driver):
            await pool.fetch(
                "https://a.example/", proxy=_proxy(), domain="a.example", session_id="s1"
            )

        assert seen == {"xvfb_locked": False, "permits_left": 3}
        assert budget.BROWSER_SEMAPHORE._value == 4  # released after, not held while parked

    @pytest.mark.asyncio
    async def test_a_failed_navigation_discards_the_driver_and_frees_the_permit(self, _budget):
        budget = _budget
        pool = BotasaurusPool(tenant_id=TENANT, config=BotasaurusConfig())
        driver = _fake_driver()
        driver.google_get.side_effect = RuntimeError("nav failed")
        with (
            patch("botasaurus.browser.Driver", return_value=driver),
            pytest.raises(RuntimeError, match="nav failed"),
        ):
            await pool.fetch(
                "https://a.example/", proxy=_proxy(), domain="a.example", session_id="s1"
            )
        driver.close.assert_called_once()
        assert pool._entries == []
        assert budget.BROWSER_SEMAPHORE._value == 4

    @pytest.mark.asyncio
    async def test_a_fetched_driver_is_parked_on_a_blank_page(self, _budget):
        """Round 65 — a parked driver holds no permit or host seat, so it must
        not keep the last page's scripts running while it waits."""
        pool = BotasaurusPool(tenant_id=TENANT, config=BotasaurusConfig())
        driver = _fake_driver()
        with patch("botasaurus.browser.Driver", return_value=driver):
            html = await pool.fetch(
                "https://a.example/", proxy=_proxy(), domain="a.example", session_id="s1"
            )
        assert html == "<html>fresh</html>"
        driver.get.assert_called_once_with("about:blank")
        assert len(pool._entries) == 1 and not pool._entries[0].busy

    @pytest.mark.asyncio
    async def test_without_parking_every_driver_is_closed_after_its_fetch(self, _budget):
        """Round 66 — under host admission a parked driver runs outside the
        host budget (live: 8 seats, 20 browsers), so none is kept."""
        pool = BotasaurusPool(tenant_id=TENANT, config=BotasaurusConfig(), park_drivers=False)
        driver = _fake_driver()
        with patch("botasaurus.browser.Driver", return_value=driver):
            html = await pool.fetch(
                "https://a.example/", proxy=_proxy(), domain="a.example", session_id="s1"
            )
        assert html == "<html>fresh</html>"
        driver.get.assert_not_called()  # not parked on about:blank first
        driver.close.assert_called_once()
        assert pool._entries == []

    @pytest.mark.asyncio
    async def test_a_driver_that_cannot_park_is_closed_but_the_page_is_kept(self, _budget):
        budget = _budget
        pool = BotasaurusPool(tenant_id=TENANT, config=BotasaurusConfig())
        driver = _fake_driver()
        driver.get.side_effect = RuntimeError("tab gone")
        with patch("botasaurus.browser.Driver", return_value=driver):
            html = await pool.fetch(
                "https://a.example/", proxy=_proxy(), domain="a.example", session_id="s1"
            )
        assert html == "<html>fresh</html>"
        driver.close.assert_called_once()
        assert pool._entries == []
        assert budget.BROWSER_SEMAPHORE._value == 4

    @pytest.mark.asyncio
    async def test_a_fetch_waits_when_every_driver_is_busy(self):
        import asyncio as _asyncio
        import threading

        pool = BotasaurusPool(tenant_id=TENANT, config=BotasaurusConfig(max_pooled_drivers=1))
        release = threading.Event()
        first = _fake_driver()
        first.google_get.side_effect = lambda *a, **k: release.wait(2)
        second = _fake_driver(html="<html>second</html>")
        with patch("botasaurus.browser.Driver", side_effect=[first, second]):
            busy = _asyncio.create_task(
                pool.fetch(
                    "https://a.example/", proxy=_proxy(), domain="a.example", session_id="s1"
                )
            )
            await _asyncio.sleep(0.05)
            waiting = _asyncio.create_task(
                pool.fetch(
                    "https://b.example/", proxy=_proxy(), domain="b.example", session_id="s1"
                )
            )
            await _asyncio.sleep(0.05)
            assert not waiting.done()
            release.set()
            await _asyncio.wait_for(busy, timeout=2)
            assert await _asyncio.wait_for(waiting, timeout=2) == "<html>second</html>"
        first.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_shutdown_closes_every_driver(self):
        pool = BotasaurusPool(tenant_id=TENANT, config=BotasaurusConfig(max_pooled_drivers=2))
        a, b = _fake_driver(), _fake_driver()
        with patch("botasaurus.browser.Driver", side_effect=[a, b]):
            await pool.fetch(
                "https://a.example/", proxy=_proxy(), domain="a.example", session_id="s1"
            )
            await pool.fetch(
                "https://b.example/", proxy=_proxy(), domain="b.example", session_id="s1"
            )
        await pool.shutdown()
        a.close.assert_called_once()
        b.close.assert_called_once()
        assert pool._entries == []


class _FakeKeeper:
    """Stands in for orchestrator/host_capacity.py::SeatKeeper (round 67)."""

    def __init__(self, seats=("seat-1", "seat-2")):
        self._seats = list(seats)
        self.released: list[str] = []
        self.reclaimers: list[object] = []

    def register_reclaimer(self, reclaim):
        self.reclaimers.append(reclaim)

    def retain(self):
        return self._seats.pop(0) if self._seats else None

    async def discard(self, seat):
        self.released.append(seat)


class TestParkedDriversKeepTheirHostSeat:
    """Round 67 — a parked driver keeps the seat of the fetch that launched
    it, instead of round 66's close-on-release under host admission."""

    @pytest.mark.asyncio
    async def test_the_pool_offers_the_keeper_a_driver_to_reclaim(self):
        keeper = _FakeKeeper()
        pool = BotasaurusPool(tenant_id=TENANT, config=BotasaurusConfig(), seat_keeper=keeper)
        assert keeper.reclaimers == [pool._close_parked]

    @pytest.mark.asyncio
    async def test_a_parked_driver_carries_its_seat(self):
        keeper = _FakeKeeper()
        pool = BotasaurusPool(tenant_id=TENANT, config=BotasaurusConfig(), seat_keeper=keeper)
        with patch("botasaurus.browser.Driver", return_value=_fake_driver()):
            await pool.fetch(
                "https://a.example/1", proxy=_proxy(), domain="a.example", session_id="s1"
            )
        assert [e.seat for e in pool._entries] == ["seat-1"]
        assert keeper.released == []

    @pytest.mark.asyncio
    async def test_a_driver_with_no_seat_to_keep_is_closed(self):
        keeper = _FakeKeeper(seats=())
        pool = BotasaurusPool(tenant_id=TENANT, config=BotasaurusConfig(), seat_keeper=keeper)
        driver = _fake_driver()
        with patch("botasaurus.browser.Driver", return_value=driver):
            await pool.fetch(
                "https://a.example/1", proxy=_proxy(), domain="a.example", session_id="s1"
            )
        assert pool._entries == []
        driver.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_reusing_a_parked_driver_hands_its_seat_back(self):
        keeper = _FakeKeeper()
        pool = BotasaurusPool(tenant_id=TENANT, config=BotasaurusConfig(), seat_keeper=keeper)
        driver = _fake_driver()
        with patch("botasaurus.browser.Driver", return_value=driver) as driver_cls:
            await pool.fetch(
                "https://a.example/1", proxy=_proxy(), domain="a.example", session_id="s1"
            )
            await pool.fetch(
                "https://a.example/2", proxy=_proxy(), domain="a.example", session_id="s1"
            )
        # One launch, two fetches: the second reused the parked driver and
        # gave its seat back, then parked it again on its own seat.
        driver_cls.assert_called_once()
        assert keeper.released == ["seat-1"]
        assert [e.seat for e in pool._entries] == ["seat-2"]

    @pytest.mark.asyncio
    async def test_a_paid_gateway_driver_is_closed_not_parked(self):
        """A gateway session is single-use (fresh sessid per attempt)."""
        keeper = _FakeKeeper()
        pool = BotasaurusPool(tenant_id=TENANT, config=BotasaurusConfig(), seat_keeper=keeper)
        gateway = Proxy(
            id=-1,
            ip="gw.example",
            port=823,
            protocol=ProxyProtocol.HTTP,
            username="u__sessid.1",
            password="p",
            source="paid_gateway",
        )
        driver = _fake_driver()
        with patch("botasaurus.browser.Driver", return_value=driver):
            await pool.fetch(
                "https://a.example/1", proxy=gateway, domain="a.example", session_id="s1"
            )

        driver.close.assert_called_once()
        assert pool._entries == []
        assert keeper._seats == ["seat-1", "seat-2"]

    @pytest.mark.asyncio
    async def test_the_keeper_can_reclaim_an_idle_driver(self):
        keeper = _FakeKeeper()
        pool = BotasaurusPool(tenant_id=TENANT, config=BotasaurusConfig(), seat_keeper=keeper)
        driver = _fake_driver()
        with patch("botasaurus.browser.Driver", return_value=driver):
            await pool.fetch(
                "https://a.example/1", proxy=_proxy(), domain="a.example", session_id="s1"
            )

        assert await pool._close_parked() is True
        assert keeper.released == ["seat-1"]
        assert pool._entries == []
        driver.close.assert_called_once()
        # Nothing parked any more: real contention, not something to reclaim.
        assert await pool._close_parked() is False

    @pytest.mark.asyncio
    async def test_the_keeper_can_reclaim_the_driver_holding_a_given_seat(self):
        """A lapsed seat must close the driver that held it, not whichever
        parked driver happens to be oldest."""
        keeper = _FakeKeeper()
        pool = BotasaurusPool(tenant_id=TENANT, config=BotasaurusConfig(), seat_keeper=keeper)
        a, b = _fake_driver(), _fake_driver()
        with patch("botasaurus.browser.Driver", side_effect=[a, b]):
            await pool.fetch(
                "https://a.example/1", proxy=_proxy(), domain="a.example", session_id="s1"
            )
            await pool.fetch(
                "https://b.example/1", proxy=_proxy(), domain="b.example", session_id="s1"
            )

        assert await pool._close_parked("seat-missing") is False
        assert await pool._close_parked("seat-2") is True
        b.close.assert_called_once()
        a.close.assert_not_called()
        assert [e.seat for e in pool._entries] == ["seat-1"]

    @pytest.mark.asyncio
    async def test_job_end_gives_every_seat_back(self):
        keeper = _FakeKeeper()
        pool = BotasaurusPool(tenant_id=TENANT, config=BotasaurusConfig(), seat_keeper=keeper)
        with patch("botasaurus.browser.Driver", return_value=_fake_driver()):
            await pool.fetch(
                "https://a.example/1", proxy=_proxy(), domain="a.example", session_id="s1"
            )

        await pool.shutdown()

        assert keeper.released == ["seat-1"]
