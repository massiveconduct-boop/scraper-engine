# tests/unit/test_botasaurus_pool.py
"""Round 26: BotasaurusPool holds a raw botasaurus Driver we construct and
key ourselves (proxy+domain match), never botasaurus's own unkeyed
reuse_driver=True pool (see browser/botasaurus_pool.py's module docstring for
why). No real Chrome/botasaurus driver launches here — botasaurus.browser.Driver
is patched out; these tests verify the reuse/eviction/close wiring, not
botasaurus's own browser automation."""

from unittest.mock import MagicMock, patch

import pytest

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
