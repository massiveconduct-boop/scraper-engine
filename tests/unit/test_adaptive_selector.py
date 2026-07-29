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
