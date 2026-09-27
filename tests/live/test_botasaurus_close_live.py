# tests/live/test_botasaurus_close_live.py
"""Live test — requires a real Botasaurus/Chromium launch and Xvfb.

2026-09-28: workers had built up 547 chromium and 120 Xvfb processes. Found
live: with tiny_profile on (our default), a Driver whose Chrome has died
fails its own close() in save_cookies ("[Errno 111] Connection refused") and
never stops its Xvfb display. A mocked Driver cannot show that the display
and Chrome are really gone afterwards; this launches both and checks.
No network: the page is about:blank.
"""

from __future__ import annotations

import os
import signal
import time
import uuid

import psutil
import pytest


def _live_descendants() -> dict[str, int]:
    from scraper_engine.core.leftover_processes import process_kind

    counts: dict[str, int] = {}
    for proc in psutil.Process().children(recursive=True):
        try:
            if proc.status() == psutil.STATUS_ZOMBIE:
                continue
            kind = process_kind(proc.name())
        except psutil.Error:
            continue
        if kind in ("chromium", "xvfb"):
            counts[kind] = counts.get(kind, 0) + 1
    return counts


@pytest.mark.live
def test_driver_whose_chrome_died_leaves_no_chrome_or_xvfb():
    from botasaurus.browser import Driver
    from botasaurus.user_agent import UserAgent
    from botasaurus.window_size import WindowSize

    from scraper_engine.browser._botasaurus_close import close_driver

    before = _live_descendants()
    # The same launch settings as browser/botasaurus_pool.py with the
    # default config (tiny_profile + hashed fingerprint need a profile).
    driver = Driver(
        headless=False,
        enable_xvfb_virtual_display=True,
        profile=f"leaktest-{uuid.uuid4().hex[:8]}",
        tiny_profile=True,
        remove_default_browser_check_argument=True,
        user_agent=UserAgent.HASHED,
        window_size=WindowSize.HASHED,
    )
    driver.get("about:blank")
    assert _live_descendants().get("xvfb", 0) > before.get("xvfb", 0)

    os.kill(driver._browser._process_pid, signal.SIGKILL)  # Chrome crashes
    time.sleep(1)
    close_driver(driver)

    deadline = time.monotonic() + 10
    while _live_descendants() != before and time.monotonic() < deadline:
        time.sleep(0.2)
    assert _live_descendants() == before
