# tests/unit/test_markdown_fallback.py
"""Local HTML->markdown fallback — used when Firecrawl isn't configured
(round 33, see services/markdown_fallback.py's docstring)."""

import pytest

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


def test_html_to_markdown_converts_deep_wrapper_chain_without_fallback(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Live-caught: a real article page's DOM was deeply nested enough
    (framework-generated layout wrapper divs — divitis) that markdownify's
    recursive tree walk raised RecursionError, which (before round 42's
    fix) had zero handling here and crashed the entire batch job.

    A wrapper-chain like this carries zero markdown-relevant content of
    its own, so the actual fix must flatten it and produce a real,
    successful conversion — not merely degrade to the plain-text fallback.
    """
    depth = 6000
    html = "<div>" * depth + "real content" + "</div>" * depth
    with caplog.at_level("WARNING"):
        md = html_to_markdown(html)
    assert md == "real content"
    assert "markdown_conversion_recursion_limit_exceeded" not in caplog.text


def test_html_to_markdown_converts_deep_non_flattenable_nesting(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Wrappers that carry their own text can't be safely unwrapped (doing
    so would drop content), so deep nesting built from those must still be
    handled by the large-stack conversion path — successfully, not via the
    plain-text fallback."""
    depth = 3000
    html = "".join(f"<div>x{i}" for i in range(depth)) + "".join(
        "</div>" for _ in range(depth)
    )
    with caplog.at_level("WARNING"):
        md = html_to_markdown(html)
    assert "x0" in md
    assert f"x{depth - 1}" in md
    assert "markdown_conversion_recursion_limit_exceeded" not in caplog.text


def test_html_to_markdown_still_falls_back_beyond_raised_limit(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Genuinely pathological nesting — deeper than even the raised
    recursion ceiling can safely absorb — must still degrade gracefully to
    the plain-text fallback rather than crashing the job. This is the
    true last-resort net, not the primary mechanism."""
    depth = 60000
    html = "".join(f"<div>x{i}" for i in range(depth)) + "".join(
        "</div>" for _ in range(depth)
    )
    with caplog.at_level("WARNING"):
        md = html_to_markdown(html)
    assert "x0" in md
    assert "markdown_conversion_recursion_limit_exceeded" in caplog.text


def test_html_to_markdown_degrades_gracefully_when_large_stack_unsupported(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """threading.stack_size() is documented as unsupported on some
    platforms. If setting it raises, conversion must still succeed on a
    default-stack thread rather than erroring out."""
    import threading

    from scraper_engine.services import markdown_fallback as module

    original = threading.stack_size()

    def fake_stack_size(size: int = 0) -> int:
        if size and size != original:
            raise RuntimeError("stack_size not supported on this platform")
        return original

    monkeypatch.setattr(module.threading, "stack_size", fake_stack_size)
    with caplog.at_level("WARNING"):
        md = html_to_markdown("<p>ordinary content</p>")
    assert "ordinary content" in md
    assert "markdown_conversion_large_stack_unsupported" in caplog.text


def test_html_to_markdown_restores_recursion_limit_after_call() -> None:
    """The temporary sys.setrecursionlimit() bump must not leak past this
    call — a process-wide change here would affect unrelated code."""
    import sys

    original = sys.getrecursionlimit()
    html_to_markdown("<p>ordinary content</p>")
    assert sys.getrecursionlimit() == original
