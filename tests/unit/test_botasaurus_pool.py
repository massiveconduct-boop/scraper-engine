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
    async def test_second_same_domain_fetch_reuses_driver_via_requests_get(self):
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
        driver.requests.get.assert_called_once_with("https://a.example/2")
        driver.close.assert_not_called()
        assert html == "<html>reused</html>"

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
        autoscroll.assert_called_once_with(driver, max_passes=3, wait_ms=200)
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
    async def test_reuse_fetch_never_autoscrolls(self):
        """Round 58 — the reuse path (driver.requests.get, an in-page JS
        fetch()) never navigates, so scroll_passes must be ignored there
        even when configured — the visible DOM would just be the previous
        page, not the one just fetched."""
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
        autoscroll.assert_not_called()
        assert html == "<html>reused</html>"

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
