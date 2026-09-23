# browser/_botasaurus_scroll.py
"""Sync lazy-load/infinite-scroll autoscroll for a Botasaurus `Driver`.

Round 57 recon found `fetcher/level_2.py::_fetch_via_botasaurus` never calls
anything from `fetcher/_content_utils.py::autoscroll` — only the Camoufox
fallback path scrolls, so a URL that succeeds on Botasaurus's first attempt
silently skips lazy-load/infinite-scroll content even when
`scroll_passes>0` is configured. `_content_utils.autoscroll` is Playwright
async (`page.evaluate`/`page.wait_for_timeout`); Botasaurus's Driver is a
sync Selenium-style API (`driver.run_js`, no awaitable timers), so this is
a separate implementation of the same height-stability algorithm rather
than a shared one.
"""

from __future__ import annotations

import random
import time
from typing import Any


def _humanize_mouse_before_pass(driver: Any) -> None:
    """Best-effort: move the mouse to a random in-viewport point before a
    scroll pass. driver.move_mouse_to_point() (driver.py:945-953) routes
    through botasaurus_humancursor when human mode is enabled (see
    config/schema.py's BotasaurusConfig.humanize_mouse docstring). Never
    raises — headless mouse-move support isn't guaranteed, and a failed
    movement must never break the scroll loop itself."""
    try:
        width, height = driver.run_js("return [window.innerWidth, window.innerHeight];")
        x = random.randint(0, max(width - 1, 0))
        y = random.randint(0, max(height - 1, 0))
        driver.move_mouse_to_point(x, y)
    except Exception:
        pass


def botasaurus_autoscroll(
    driver: Any,
    *,
    max_passes: int,
    wait_ms: int,
    stable_passes_before_stop: int = 2,
    humanize: bool = False,
) -> int:
    """Scroll to the bottom repeatedly to trigger lazy-load / infinite scroll.

    Same algorithm as `_content_utils.autoscroll`: scroll to bottom, wait,
    re-read `document.body.scrollHeight`, stop once height has stayed flat
    for `stable_passes_before_stop` consecutive passes or `max_passes` is
    hit. Never raises — a page that can't be scrolled just yields 0.

    humanize=True moves the mouse to a random in-viewport point before each
    pass (round 60) — best-effort, never blocks the scroll itself on failure.
    """
    if max_passes <= 0:
        return 0
    passes = 0
    try:
        last_height = driver.run_js("return document.body.scrollHeight;")
    except Exception:
        return 0
    stable = 0
    for _ in range(max_passes):
        try:
            if humanize:
                _humanize_mouse_before_pass(driver)
            driver.run_js("window.scrollTo(0, document.body.scrollHeight);")
            time.sleep(wait_ms / 1000)
            new_height = driver.run_js("return document.body.scrollHeight;")
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
