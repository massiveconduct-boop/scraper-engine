# tests/unit/test_adaptive_selector.py
"""AdaptiveSelector — bs4 + regex-fallback extraction paths. Was 0% covered."""

import pytest

from scraper_engine.fetcher.adaptive_selector import AdaptiveSelector

_HTML = """
<html>
<head><title> My Page Title </title></head>
<body>
<main>
<p>{filler}</p>
<a href="/a">a</a>
<a href="/b">b</a>
</main>
</body>
</html>
""".format(filler="word " * 30)

_HTML_NO_MATCH = "<html><head></head><body><p>short</p></body></html>"


class TestAdaptiveSelectorInit:
    def test_bs4_available_true_when_importable(self):
        selector = AdaptiveSelector()
        assert selector._bs4_available is True

    def test_bs4_available_false_when_not_importable(self, monkeypatch):
        import builtins

        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "bs4":
                raise ImportError("no bs4")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", fake_import)
        selector = AdaptiveSelector()
        assert selector._bs4_available is False


class TestExtractWithBs4:
    @pytest.mark.asyncio
    async def test_extracts_content_title_and_links(self):
        selector = AdaptiveSelector()
        result = await selector.extract(_HTML)

        assert result["selector_used"] == "main"
        assert "word" in result["content"]
        assert result["title"] == "My Page Title"
        assert result["links"] == ["/a", "/b"]

    @pytest.mark.asyncio
    async def test_no_selector_matches_over_100_chars(self):
        selector = AdaptiveSelector()
        result = await selector.extract(_HTML_NO_MATCH)

        assert "content" not in result
        assert "links" not in result

    @pytest.mark.asyncio
    async def test_schema_passthrough(self):
        selector = AdaptiveSelector()
        schema = {"field": "value"}
        result = await selector.extract(_HTML, schema=schema)

        assert result["schema"] == schema


class TestExtractRegexFallback:
    @pytest.mark.asyncio
    async def test_fallback_extracts_content_and_title(self):
        selector = AdaptiveSelector()
        selector._bs4_available = False

        result = await selector.extract(_HTML)

        assert "content" in result
        assert "<" not in result["content"]
        assert result["title"] == "My Page Title"

    @pytest.mark.asyncio
    async def test_fallback_no_title_tag(self):
        selector = AdaptiveSelector()
        selector._bs4_available = False

        result = await selector.extract("<html><body><p>plain text</p></body></html>")

        assert "title" not in result
        assert "plain text" in result["content"]


class TestLinkExtraction:
    """Round 63 — what `links` used to be was `hrefs[:100]` in raw DOM order:
    relative, un-deduped, and truncated at 100.

    Those three only bite together, and they did on a real Jumia catalog page
    rendered at L3: the hydrated nav mega-menu emits ~100 links before the
    first product, so the whole budget went to site navigation and not one
    product URL came back. The identical page captured earlier at L2, before
    that hydration, returned them all — which read as "L3 loses product
    links" when it was the cap.
    """

    @pytest.mark.asyncio
    async def test_jumia_shape_nav_menu_no_longer_crowds_out_products(self):
        nav = "".join(f'<a href="/category/{i}">c{i}</a>' for i in range(100))
        products = "".join(f'<a href="/product-{i}.html">p{i}</a>' for i in range(24))
        html = f"<html><body><main>{'x' * 200}{nav}{products}</main></body></html>"

        result = await AdaptiveSelector().extract(html, base_url="https://www.jumia.com.ng/")

        links = result["links"]
        assert len(links) == 124
        product_links = [link for link in links if "product-" in link]
        assert len(product_links) == 24
        assert product_links[0] == "https://www.jumia.com.ng/product-0.html"

    @pytest.mark.asyncio
    async def test_relative_links_are_absolutised_against_the_page(self):
        html = (
            "<html><body><main>" + "x" * 200 + '<a href="/a">a</a>'
            '<a href="b.html">b</a><a href="https://other.example/c">c</a>'
            "</main></body></html>"
        )
        result = await AdaptiveSelector().extract(
            html, base_url="https://site.example/shop/index.html"
        )
        assert result["links"] == [
            "https://site.example/a",
            "https://site.example/shop/b.html",
            "https://other.example/c",
        ]

    @pytest.mark.asyncio
    async def test_without_a_base_url_links_stay_as_authored(self):
        html = "<html><body><main>" + "x" * 200 + '<a href="/a">a</a></main></body></html>'
        result = await AdaptiveSelector().extract(html)
        assert result["links"] == ["/a"]

    @pytest.mark.asyncio
    async def test_duplicates_are_dropped_preserving_first_seen_order(self):
        html = (
            "<html><body><main>" + "x" * 200 + '<a href="/a">1</a><a href="/b">2</a>'
            '<a href="/a">3</a></main></body></html>'
        )
        result = await AdaptiveSelector().extract(html)
        assert result["links"] == ["/a", "/b"]

    @pytest.mark.asyncio
    async def test_non_page_schemes_and_bare_fragments_are_dropped(self):
        html = (
            "<html><body><main>" + "x" * 200 + '<a href="javascript:void(0)">j</a>'
            '<a href="MAILTO:x@y.z">m</a><a href="tel:+123">t</a>'
            '<a href="#top">f</a><a href="  ">blank</a><a href="/real">r</a>'
            "</main></body></html>"
        )
        result = await AdaptiveSelector().extract(html)
        assert result["links"] == ["/real"]

    @pytest.mark.asyncio
    async def test_cap_still_bounds_a_pathological_page(self):
        html = (
            "<html><body><main>"
            + "x" * 200
            + "".join(f'<a href="/{i}">{i}</a>' for i in range(50))
            + "</main></body></html>"
        )
        result = await AdaptiveSelector(max_links=10).extract(html)
        assert len(result["links"]) == 10
