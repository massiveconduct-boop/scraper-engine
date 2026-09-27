"""Close a botasaurus Driver so nothing it started outlives it.

`Driver.close()` is not safe to trust on its own. With `tiny_profile` on (our
default) it first calls `save_cookies()` over CDP, and only then closes the
browser, the Xvfb display and the local auth proxy. When Chrome has already
died (crash, OOM kill) that CDP call raises `[Errno 111] Connection refused`
and none of the rest runs: the Xvfb display stays up and Chrome's dead
processes are never reaped. Reproduced live in a worker container (2026-09-28):
after a failed close, one `Xvfb` and every `chromium` process were still there.
Workers had built up 120 Xvfb and 547 chromium processes this way.

`close_driver()` tries the normal close first, then kills whatever that left.
`finish_close()` is the second half on its own, for the @browser decorator
path (fetcher/botasaurus_wrapper.py), where botasaurus runs the close itself.
"""

from __future__ import annotations

import contextlib
import logging
from typing import Any

from scraper_engine.browser._xvfb_cleanup import cleanup_stale_display

logger = logging.getLogger(__name__)

# Chrome gets SIGKILL, so it only has to be reaped; this bounds that wait.
_PROCESS_WAIT_SECONDS = 5


def close_driver(driver: Any) -> None:
    """Close `driver` normally, then kill whatever the close did not."""
    try:
        driver.close()
    except Exception as exc:
        logger.warning(
            "botasaurus_close_failed error=%s — killing its browser and display directly",
            exc,
        )
    finish_close(driver)


def finish_close(driver: Any) -> None:
    """Kill the Chrome process, Xvfb display and local proxy a Driver still holds.

    Reads what is still running, so it is safe after a close that worked (it
    then does nothing but the lock-file cleanup) and after one that failed
    partway. Never raises.
    """
    browser: Any = getattr(driver, "_browser", None)
    config: Any = getattr(driver, "config", None)
    process = getattr(browser, "_process", None)
    if process is not None:
        # botasaurus's Browser.close() clears _process once Chrome is gone and
        # only then closes the config (display + local proxy). Still set means
        # it never got that far, so both are ours to close.
        with contextlib.suppress(Exception):
            process.kill()
        with contextlib.suppress(Exception):
            process.wait(timeout=_PROCESS_WAIT_SECONDS)
        with contextlib.suppress(Exception):
            browser._process = None
        with contextlib.suppress(Exception):
            config.close()
    display = getattr(config, "_display", None)
    with contextlib.suppress(Exception):
        if display is not None and display.is_alive():
            display.stop()
    with contextlib.suppress(Exception):
        cleanup_stale_display(driver)
