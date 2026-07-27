# fetcher/_content_utils.py
"""Shared page-content helpers for Level2Fetcher and Level3Fetcher.

Both fetchers face the same two problems after navigating to a challenge page:
  1. page.content() can raise mid-navigation (ProtocolError) — must be guarded.
  2. The page may still be showing an unsolved challenge interstitial when first
     read — must be polled until ChallengeDetector says it's real content, up to
     a bounded ceiling.

Round 12.3–12.4 built and proved this for Level3Fetcher. Round 14 needed the
identical behaviour in Level2Fetcher (its lack of it was the ~timing-race
flakiness). Rather than duplicate the logic a second time, it lives here once —
the same "one source of truth" principle already applied to ChallengeDetector.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from fetcher.challenge_detector import ChallengeDetector

# `page` is a Playwright/Camoufox Page — duck-typed as Any here so these shared
# helpers don't take a hard dependency on the Playwright types (which aren't
# consistently importable across the Camoufox stack). The concrete methods
# (content/evaluate/wait_for_timeout) are exercised by the live/chaos tests.


async def safe_content(page: Any) -> str | None:
    """Return page.content() or None if the page is mid-navigation.

    Calls to page.content() while the page is navigating or replacing the DOM
    can raise ProtocolError. A failed read is treated as "still unsolved, keep
    polling" by the caller — never as "solved". Increments safe_content_none_total
    on every None return so the guard's firing rate is observable in production
    (round 12.4).
    """
    try:
        content: str = await page.content()
        return content
    except Exception:
        from observability.metrics import safe_content_none_total

        safe_content_none_total.inc()
        return None


async def poll_until_solved(
    page: Any,
    challenge_detector: ChallengeDetector,
    *,
    max_total_wait_ms: int,
    retry_wait_increment_ms: int,
    waited_ms: int = 0,
) -> str | None:
    """Poll page content until it stops looking like a challenge interstitial.

    Reads content (guarded), and while it is either an unreadable/mid-navigation
    None OR still classified as a challenge page, waits retry_wait_increment_ms
    and re-reads — up to the max_total_wait_ms ceiling. A None read is treated as
    "keep waiting", never as "solved".

    Args:
        waited_ms: time already elapsed before the first read (e.g. an L3 fixed
            post-load delay). L2 passes 0 (it relies on the poll loop entirely).

    Returns the last content read (real content on success, or the final
    interstitial/None if the ceiling was hit — the caller/ChallengeDetector
    downstream still gates success).
    """
    html = await safe_content(page)
    while (
        (
            html is None
            or challenge_detector.is_challenge_page(
                html, 200, short_page_is_suspect=False
            )
        )
        and waited_ms < max_total_wait_ms
    ):
        await page.wait_for_timeout(retry_wait_increment_ms)
        waited_ms += retry_wait_increment_ms
        html = await safe_content(page)
    return html


async def autoscroll(
    page: Any,
    *,
    max_passes: int,
    wait_ms: int,
    stable_passes_before_stop: int = 2,
) -> int:
    """Scroll to the bottom repeatedly to trigger lazy-load / infinite scroll.

    Reads document height, scrolls to the bottom, waits wait_ms for new content
    to load, re-reads height, and repeats until either max_passes is reached or
    the height has stayed flat for `stable_passes_before_stop` CONSECUTIVE passes.

    The consecutive-stable requirement matters: AJAX-loaded content lags the
    scroll, so a single pass can show no growth while a request is still in
    flight, and the next pass then grows (observed live on quotes.toscrape.com
    /scroll: 10→20, flat, →30). Stopping on the first flat pass abandons content
    mid-load; requiring two flat passes in a row tolerates that lag while still
    early-exiting quickly on pages that genuinely have nothing more to load.

    Returns the number of passes actually performed. Never raises — a page that
    can't be scrolled just yields 0 and stops.
    """
    if max_passes <= 0:
        return 0
    passes = 0
    try:
        last_height = await page.evaluate("() => document.body.scrollHeight")
    except Exception:
        return 0
    stable = 0
    for _ in range(max_passes):
        try:
            await page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
            await page.wait_for_timeout(wait_ms)
            new_height = await page.evaluate("() => document.body.scrollHeight")
        except Exception:
            break
        passes += 1
        if new_height <= last_height:
            stable += 1
            if stable >= stable_passes_before_stop:
                break  # flat for N consecutive passes — genuinely fully loaded
        else:
            stable = 0
            last_height = new_height
    return passes
