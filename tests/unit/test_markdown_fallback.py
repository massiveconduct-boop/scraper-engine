# tests/unit/test_markdown_fallback.py
"""Local HTML->markdown fallback — used when Firecrawl isn't configured
(round 33, see services/markdown_fallback.py's docstring)."""

from scraper_engine.services.markdown_fallback import html_to_markdown


def test_html_to_markdown_converts_heading_and_text() -> None:
    html = "<html><body><h1>Title</h1><p>Body text</p></body></html>"
    md = html_to_markdown(html)
    assert "# Title" in md
    assert "Body text" in md


def test_html_to_markdown_converts_links() -> None:
    html = '<a href="https://example.com">link text</a>'
    md = html_to_markdown(html)
    assert "[link text](https://example.com)" in md


def test_html_to_markdown_strips_script_and_style() -> None:
    html = "<html><body><script>alert(1)</script><style>.x{}</style><p>visible</p></body></html>"
    md = html_to_markdown(html)
    assert "alert" not in md
    assert "visible" in md


def test_html_to_markdown_handles_empty_string() -> None:
    assert html_to_markdown("") == ""
