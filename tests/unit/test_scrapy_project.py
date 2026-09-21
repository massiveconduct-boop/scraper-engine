"""scrapy_project — the middlewares and pipeline every POST /v1/crawl loads.

Round 64. These ran on a live endpoint with zero tests (round-62 audit T1),
and three of them turned out to be stubs: the proxy middleware sent every
request from the server's own IP, the dedup pipeline deduplicated nothing,
and nothing SSRF-checked a redirect hop (design invariant #4).
"""

import socket
from unittest.mock import MagicMock, patch

import pytest
from scrapy import Request
from scrapy.exceptions import DropItem, IgnoreRequest
from scrapy.http import Response
from scrapy.settings import Settings

from scraper_engine.scrapy_project import settings as project_settings
from scraper_engine.scrapy_project.middlewares.proxy_middleware import ProxyMiddleware
from scraper_engine.scrapy_project.middlewares.ssrf_middleware import SSRFMiddleware
from scraper_engine.scrapy_project.pipelines.dedup_pipeline import DedupPipeline


def _crawler(stats=True, **settings):
    crawler = MagicMock()
    crawler.stats = MagicMock() if stats else None
    crawler.settings = Settings(settings)
    return crawler


SPIDER = MagicMock()


class TestSSRFMiddleware:
    def test_a_private_target_is_ignored_and_counted(self):
        crawler = _crawler()
        mw = SSRFMiddleware.from_crawler(crawler)
        with (
            patch("socket.getaddrinfo", return_value=[(2, 1, 6, "", ("169.254.169.254", 0))]),
            pytest.raises(IgnoreRequest),
        ):
            mw.process_request(Request("http://metadata.example/latest"), SPIDER)
        crawler.stats.inc_value.assert_called_once_with("ssrf/blocked")

    def test_a_redirect_hop_is_checked_like_any_request(self):
        """Scrapy's RedirectMiddleware re-issues each hop as a new Request
        through the downloader middlewares, so the hop hits process_request —
        a public seed that 302s to cloud metadata is stopped here."""
        mw = SSRFMiddleware.from_crawler(_crawler())
        seed = Request("https://public.example/")
        hop = seed.replace(url="http://169.254.169.254/latest/meta-data/")
        with (
            patch("socket.getaddrinfo", return_value=[(2, 1, 6, "", ("169.254.169.254", 0))]),
            pytest.raises(IgnoreRequest),
        ):
            mw.process_request(hop, SPIDER)

    def test_a_public_target_passes(self):
        mw = SSRFMiddleware.from_crawler(_crawler())
        with patch("socket.getaddrinfo", return_value=[(2, 1, 6, "", ("93.184.216.34", 0))]):
            assert mw.process_request(Request("https://example.com/"), SPIDER) is None

    def test_an_unresolvable_host_is_ignored_without_stats(self):
        mw = SSRFMiddleware.from_crawler(_crawler(stats=False))
        with (
            patch("socket.getaddrinfo", side_effect=socket.gaierror("nxdomain")),
            pytest.raises(IgnoreRequest),
        ):
            mw.process_request(Request("https://nx.example/"), SPIDER)


class TestProxyMiddleware:
    def test_attaches_the_leased_proxy(self):
        mw = ProxyMiddleware.from_crawler(_crawler(CRAWL_PROXY_URL="http://u:p@gw.example:823"))
        request = Request("https://example.com/")
        mw.process_request(request, SPIDER)
        assert request.meta["proxy"] == "http://u:p@gw.example:823"

    def test_leaves_an_explicit_per_request_proxy_alone(self):
        mw = ProxyMiddleware.from_crawler(_crawler(CRAWL_PROXY_URL="http://leased:1"))
        request = Request("https://example.com/", meta={"proxy": "http://explicit:2"})
        mw.process_request(request, SPIDER)
        assert request.meta["proxy"] == "http://explicit:2"

    def test_no_leased_proxy_sets_nothing(self):
        """The old stub set meta["proxy"] = None, which Scrapy treats as
        'no proxy' — the silent direct connection this round removed."""
        mw = ProxyMiddleware.from_crawler(_crawler())
        request = Request("https://example.com/")
        mw.process_request(request, SPIDER)
        assert "proxy" not in request.meta

    @pytest.mark.parametrize("status", [403, 429, 503])
    def test_block_statuses_are_counted(self, status):
        crawler = _crawler()
        mw = ProxyMiddleware.from_crawler(crawler)
        request = Request("https://example.com/")
        response = Response("https://example.com/", status=status, request=request)
        assert mw.process_response(request, response, SPIDER) is response
        crawler.stats.inc_value.assert_called_once_with("proxy/blocked")

    def test_ok_status_is_not_counted(self):
        crawler = _crawler()
        mw = ProxyMiddleware.from_crawler(crawler)
        request = Request("https://example.com/")
        mw.process_response(request, Response("https://example.com/", status=200), SPIDER)
        crawler.stats.inc_value.assert_not_called()

    def test_block_without_stats_does_not_crash(self):
        mw = ProxyMiddleware.from_crawler(_crawler(stats=False))
        request = Request("https://example.com/")
        mw.process_response(request, Response("https://example.com/", status=403), SPIDER)
        mw.process_exception(request, RuntimeError("x"), SPIDER)

    def test_a_request_refused_upstream_is_not_a_proxy_error(self):
        crawler = _crawler()
        mw = ProxyMiddleware.from_crawler(crawler)
        mw.process_exception(Request("http://169.254.169.254/"), IgnoreRequest("ssrf"), SPIDER)
        crawler.stats.inc_value.assert_not_called()

    def test_exceptions_are_counted(self):
        crawler = _crawler()
        mw = ProxyMiddleware.from_crawler(crawler)
        mw.process_exception(Request("https://example.com/"), RuntimeError("reset"), SPIDER)
        crawler.stats.inc_value.assert_called_once_with("proxy/errors")


class TestDedupPipeline:
    def test_a_repeated_url_is_dropped(self):
        crawler = _crawler()
        pipeline = DedupPipeline.from_crawler(crawler)
        first = {"url": "https://example.com/a", "title": "A"}
        assert pipeline.process_item(first, SPIDER) is first
        with pytest.raises(DropItem):
            pipeline.process_item({"url": "https://example.com/a", "title": "A again"}, SPIDER)
        crawler.stats.inc_value.assert_called_once_with("pipeline/dedup_dropped")

    def test_distinct_urls_pass(self):
        pipeline = DedupPipeline.from_crawler(_crawler(stats=False))
        for url in ("https://example.com/a", "https://example.com/b"):
            pipeline.process_item({"url": url}, SPIDER)

    def test_duplicate_without_stats_still_drops(self):
        pipeline = DedupPipeline.from_crawler(_crawler(stats=False))
        pipeline.process_item({"url": "u"}, SPIDER)
        with pytest.raises(DropItem):
            pipeline.process_item({"url": "u"}, SPIDER)


class TestSettings:
    def test_ssrf_runs_before_the_proxy(self):
        mws = project_settings.DOWNLOADER_MIDDLEWARES
        ssrf = mws["scraper_engine.scrapy_project.middlewares.ssrf_middleware.SSRFMiddleware"]
        proxy = mws["scraper_engine.scrapy_project.middlewares.proxy_middleware.ProxyMiddleware"]
        assert ssrf < proxy < 750  # 750 = Scrapy's own HttpProxyMiddleware

    def test_only_real_components_are_wired(self):
        wired = set(project_settings.DOWNLOADER_MIDDLEWARES) | set(project_settings.ITEM_PIPELINES)
        assert not any("Tenant" in path or "Storage" in path for path in wired)
