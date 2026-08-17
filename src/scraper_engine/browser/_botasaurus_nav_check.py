# browser/_botasaurus_nav_check.py
"""Detects Botasaurus/Chromium silently navigating to its own internal
network-error page instead of the real target (round 57).

Root cause: `botasaurus_driver.Driver.get()`/`google_get()` wrap a raw CDP
`Page.navigate` and only poll for `document.readyState` — they never
inspect or raise on a navigation failure. Chrome's DevTools Protocol does
not raise a Python exception for a network-level failure (DNS, connection
reset, empty response, proxy failure); Chromium instead silently renders
its own `chrome-error://chromewebdata/` interstitial as if it were a real
page, and `driver.get()` returns as if nothing went wrong. Every existing
downstream check (ChallengeDetector's vendor signatures, gateway-error
regex, Firefox-plaintext-wrapper regex) was verified to structurally miss
this — none of them expect Chromium's own UI chrome as input.

`current_url` (Chromium's own internal URL scheme) is used instead of
matching the interstitial's human-readable text: one signal covers DNS
failure, connection reset, empty response, and proxy failure alike (the
whole net::ERR_* failure class), and unlike the rendered heading/body text
it is not affected by browser UI locale.
"""

from __future__ import annotations

from typing import Any


class BotasaurusNavigationError(Exception):
    """driver.get()/google_get() silently navigated to Chromium's own
    internal network-error interstitial instead of the real target — no
    real response was ever received for this URL."""


def raise_if_navigation_failed(driver: Any, url: str) -> None:
    """Raise BotasaurusNavigationError if `driver` is currently sitting on
    Chromium's internal error page instead of a real navigation result.

    Fails open: if reading `current_url` itself raises, the check is
    skipped rather than letting a diagnostic read become a new source of
    failure — today's behavior is unchanged for that one edge case.
    """
    try:
        current = driver.current_url
    except Exception:
        return
    if isinstance(current, str) and current.startswith("chrome-error://"):
        raise BotasaurusNavigationError(
            f"Botasaurus/Chromium failed to navigate to {url!r} — landed on "
            f"its own internal error page (current_url={current!r}) instead "
            f"of the real target. No real response was ever received."
        )
