# services/markdown_fallback.py
"""Local HTML-to-markdown conversion — the default markdown source.

FirecrawlClient (firecrawl_client.py) is preferred when configured, but it
re-fetches the URL through Firecrawl's own scraper rather than converting
the HTML this process already has. Firecrawl is opt-in
(FIRECRAWL_API_KEY/FIRECRAWL_BASE_URL), so without it FetchResult.markdown
was simply left None — a caller who wants markdown always had to stand up a
separate tool first. This module needs nothing external: it converts the
already-fetched HTML in-process, so orchestrator/worker.py can populate
markdown unconditionally.
"""

from __future__ import annotations

import logging
import sys
import threading

from bs4 import BeautifulSoup
from bs4.element import Tag
from markdownify import markdownify

logger = logging.getLogger(__name__)

# Round 42 — markdownify's process_tag/process_element recurse once per
# *nesting level* of the parsed DOM (not per raw HTML byte), so a real,
# legitimately-authored article page can exceed Python's default 1000-frame
# limit long before its HTML looks unusually large. Live-caught: a real
# nairametrics.com/businessday.ng article crashed with RecursionError deep in
# markdownify's tree walk, and because html_to_markdown() had zero exception
# handling, that propagated all the way up through Worker.process_job and
# crashed the ENTIRE job.
#
# This round — round 42's fix only caught the crash; it didn't make the
# conversion actually succeed on deep real pages. The real driver, live-
# confirmed against the crashing page's markup, is framework-generated
# layout wrapper divs (divitis) nested dozens to thousands of levels deep
# with zero markdown-relevant content of their own — CSS hooks, not
# structure. _flatten_redundant_wrappers() collapses those chains
# iteratively (an explicit stack, never Python recursion) before handing
# the tree to markdownify, so markdownify only ever has to recurse across
# the DOM's *meaningfully* nested tags — for real content, a tiny fraction
# of the raw tag depth. _convert_with_large_stack() is the second layer:
# genuinely deep meaningful nesting (that flattening correctly leaves
# alone) still gets a much higher recursion ceiling, made safe by running
# on a dedicated thread with a much larger C stack — raising
# sys.setrecursionlimit() alone past a few thousand risks a real,
# uncatchable C-stack overflow (a segfault) rather than a catchable
# RecursionError, since the Python-level counter is just a soft guard
# against that. The plain-text fallback below is now a true last resort:
# it only fires for content that defeats both layers, not for the
# divitis-driven crash that was actually hit in production.
_RECURSION_LIMIT_FOR_CONVERSION = 10000
_CONVERSION_THREAD_STACK_BYTES = 64 * 1024 * 1024

# Tags markdownify actually turns into markdown syntax, or that carry
# content structure worth preserving in their own right. Anything else is
# a layout-only wrapper as far as markdown is concerned.
_MEANINGFUL_TAGS = frozenset(
    {
        "a",
        "img",
        "table",
        "thead",
        "tbody",
        "tr",
        "td",
        "th",
        "ul",
        "ol",
        "li",
        "blockquote",
        "pre",
        "code",
        "hr",
        "br",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "strong",
        "b",
        "em",
        "i",
        "p",
    }
)


def _flatten_redundant_wrappers(root: Tag) -> None:
    """Unwraps content-free single-child wrapper tags
    (``<div><div><div>real content</div></div></div>``) down to their
    innermost content.

    Traversal is post-order over an explicit stack, not recursion, so this
    pass itself can never hit Python's recursion limit regardless of how
    deep the original DOM goes. Processing children before parents means a
    whole chain collapses in one pass: by the time a wrapper's parent is
    evaluated, the wrapper has already been unwrapped, so the parent sees
    the (now promoted) grandchild directly.
    """
    stack: list[tuple[Tag, bool]] = [(root, False)]
    while stack:
        node, visited = stack.pop()
        if not visited:
            stack.append((node, True))
            for child in node.contents:
                if isinstance(child, Tag):
                    stack.append((child, False))
            continue
        if node is root or node.name in _MEANINGFUL_TAGS:
            continue
        child_tags = [c for c in node.contents if isinstance(c, Tag)]
        if len(child_tags) != 1:
            continue
        has_own_text = any(
            not isinstance(c, Tag) and str(c).strip() for c in node.contents
        )
        if has_own_text:
            continue
        node.unwrap()


def _convert_with_large_stack(html: str) -> str:
    """Runs markdownify on a dedicated thread with a much larger C stack,
    so _RECURSION_LIMIT_FOR_CONVERSION's headroom is backed by real stack
    capacity instead of just a higher soft limit. threading.stack_size()
    is unsupported on some platforms; if setting it fails, the conversion
    still runs (isolated on its own thread) at the default stack size
    rather than erroring out.
    """
    result: dict[str, str] = {}
    error: dict[str, BaseException] = {}

    def _run() -> None:
        try:
            result["value"] = str(markdownify(html, heading_style="ATX"))
        except RecursionError as exc:
            error["value"] = exc

    original_stack_size = threading.stack_size()
    original_limit = sys.getrecursionlimit()
    try:
        try:
            threading.stack_size(_CONVERSION_THREAD_STACK_BYTES)
        except (ValueError, RuntimeError):
            logger.warning("markdown_conversion_large_stack_unsupported")
        sys.setrecursionlimit(_RECURSION_LIMIT_FOR_CONVERSION)
        thread = threading.Thread(target=_run)
        thread.start()
        thread.join()
    finally:
        threading.stack_size(original_stack_size)
        sys.setrecursionlimit(original_limit)
    if "value" in error:
        raise error["value"]
    return result["value"]


def html_to_markdown(html: str) -> str:
    """markdownify's own `strip=` only skips tag *formatting* — the text
    content of a stripped tag still passes through unconverted. script/style
    bodies (JS source, CSS rules) are never real page content, so they're
    removed as nodes before conversion rather than merely left unstyled.

    Never lets a conversion failure propagate to the caller — markdown is
    explicitly a best-effort nicety (see module docstring: the non-Firecrawl
    fallback path), the same fail-soft philosophy orchestrator/worker.py
    already applies to extraction-engine calls right next to this one. On
    the rare RecursionError that survives both _flatten_redundant_wrappers
    and _convert_with_large_stack's larger ceiling, falls back to
    BeautifulSoup's plain `.get_text()` — real content, just unformatted,
    rather than nothing.
    """
    if not html:
        return ""
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style"]):
        tag.decompose()
    _flatten_redundant_wrappers(soup)
    try:
        return _convert_with_large_stack(str(soup)).strip()
    except RecursionError:
        logger.warning(
            "markdown_conversion_recursion_limit_exceeded html_len=%d — "
            "falling back to plain text extraction",
            len(html),
        )
        return str(soup.get_text(separator="\n", strip=True))
