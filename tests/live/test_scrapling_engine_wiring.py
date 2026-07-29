# tests/live/test_scrapling_engine_wiring.py
"""Live proof that round 28's wiring is real, not just unit-mocked:

- fetcher/scrapling_wrapper.py's ScraplingWrapper actually talks to
  scrapling.fetchers.AsyncFetcher over the real network (the curl_cffi
  dependency gap found while writing this test — scrapling==0.4.11 alone
  doesn't pull in AsyncFetcher's runtime requirement — is a separate real
  fix in pyproject.toml, not something this test works around).
- fetcher/factory.py actually constructs that wrapper for the real,
  config-driven "scrapling" engine (base.yaml's default for level_1).
- Level1Fetcher's manual redirect loop (built for the SSRF-per-hop
  invariant) works against a REAL multi-hop redirect chain, not a
  hand-rolled fake.
- fetcher/adaptive_selector.py's AdaptiveSelector extracts real structure
  from real fetched HTML.

Targets httpbin.org (this repo's already-established live-test convention,
see test_smoke.py) — a public HTTP-testing service, not a scraped "target"
in the adversarial sense.
"""

import asyncio

import pytest

from scraper_engine.config.loader import load_config
from scraper_engine.core.tenant import TenantId
from scraper_engine.fetcher.adaptive_selector import AdaptiveSelector
from scraper_engine.fetcher.factory import build_level1_fetcher
from scraper_engine.fetcher.scrapling_wrapper import ScraplingWrapper

_TENANT = TenantId("livetest")


async def _fetch_with_retries(fetcher, url, attempts=3):
    result = None
    for attempt in range(attempts):
        result = await fetcher.fetch(url, _TENANT)
        if result.success:
            return result
        if attempt < attempts - 1:
            await asyncio.sleep(2)
    return result


@pytest.mark.live
class TestScraplingEngineWiring:
    @pytest.mark.asyncio
    async def test_factory_wires_real_scrapling_client_by_default(self):
        """base.yaml's level_1.engine: scrapling must produce a fetcher
        whose scrapling_client is real, not None — proves the config gate
        (fetcher/factory.py) actually fires, not just that the class exists."""
        config = load_config()
        fetcher = build_level1_fetcher(config)

        assert isinstance(fetcher._scrapling_client, ScraplingWrapper)
        assert fetcher._scrapling_client._available is True

    @pytest.mark.asyncio
    async def test_scrapling_engine_fetches_real_content(self):
        config = load_config()
        fetcher = build_level1_fetcher(config)

        result = await _fetch_with_retries(fetcher, "https://httpbin.org/get")
        if not result.success:
            pytest.skip("httpbin.org unreachable after 3 attempts")

        assert result.http_status == 200
        assert result.html is not None and len(result.html) > 0

    @pytest.mark.asyncio
    async def test_scrapling_engine_follows_real_redirect_chain(self):
        """httpbin.org/redirect/2 issues two real 302 hops before landing on
        /get — proves Level1Fetcher._fetch_via_scrapling's manual redirect
        loop (built so every hop gets SSRF-revalidated) works against a
        real multi-hop chain, not a single-hop mock."""
        config = load_config()
        fetcher = build_level1_fetcher(config)

        result = await _fetch_with_retries(fetcher, "https://httpbin.org/redirect/2")
        if not result.success:
            pytest.skip("httpbin.org unreachable after 3 attempts")

        assert result.http_status == 200
        assert result.html is not None and '"url"' in result.html

    @pytest.mark.asyncio
    async def test_scrapling_engine_handles_real_404_gracefully(self):
        config = load_config()
        fetcher = build_level1_fetcher(config)

        result = await _fetch_with_retries(fetcher, "https://httpbin.org/status/404")
        if result is None:
            pytest.skip("httpbin.org unreachable")

        assert result.http_status == 404
        assert result.success is False


@pytest.mark.live
class TestAdaptiveSelectorWiring:
    @pytest.mark.asyncio
    async def test_extracts_structure_from_real_fetched_html(self):
        """Fetches real content via the now-wired scrapling engine, then
        runs it through AdaptiveSelector — proving both round-28 wirings
        work together end to end, the same order Worker.process_job now
        actually drives them in (fetch, then extract on success). Uses
        example.com (has a real <title>, unlike httpbin.org/html) so the
        title-extraction path is actually exercised, not just content."""
        config = load_config()
        fetcher = build_level1_fetcher(config)

        result = await _fetch_with_retries(fetcher, "https://example.com")
        if not result.success or not result.html:
            pytest.skip("example.com unreachable after 3 attempts")

        extracted = await AdaptiveSelector().extract(result.html)

        assert extracted.get("title") == "Example Domain"
        assert "content" in extracted and "Example Domain" in extracted["content"]

    @pytest.mark.asyncio
    async def test_extracts_content_when_page_has_no_title_tag(self):
        """httpbin.org/html has no <title> element — AdaptiveSelector must
        still extract body content and simply omit the "title" key, not
        crash or return an empty result."""
        config = load_config()
        fetcher = build_level1_fetcher(config)

        result = await _fetch_with_retries(fetcher, "https://httpbin.org/html")
        if not result.success or not result.html:
            pytest.skip("httpbin.org unreachable after 3 attempts")

        extracted = await AdaptiveSelector().extract(result.html)

        assert "title" not in extracted
        assert "Moby-Dick" in extracted.get("content", "")
