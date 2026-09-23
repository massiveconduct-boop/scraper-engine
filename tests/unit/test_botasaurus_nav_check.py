# tests/unit/test_botasaurus_nav_check.py
"""Round 57 — browser/_botasaurus_nav_check.py.

botasaurus_driver.Driver.get()/google_get() never raise for a network-level
navigation failure; Chromium silently renders its own chrome-error://
interstitial instead. raise_if_navigation_failed() is the root-cause check
that converts that silent failure into a real, classifiable exception.
"""

from __future__ import annotations

import pytest

from scraper_engine.browser._botasaurus_nav_check import (
    BotasaurusNavigationError,
    raise_if_navigation_failed,
)

URL = "https://target.example/page"


class _FakeDriver:
    def __init__(self, current_url: str) -> None:
        self.current_url = current_url


class _RaisingCurrentUrlDriver:
    @property
    def current_url(self) -> str:
        raise RuntimeError("devtools connection lost")


def test_raises_on_chrome_error_interstitial():
    driver = _FakeDriver("chrome-error://chromewebdata/")
    with pytest.raises(BotasaurusNavigationError) as exc_info:
        raise_if_navigation_failed(driver, URL)
    assert URL in str(exc_info.value)
    assert "chrome-error://chromewebdata/" in str(exc_info.value)


def test_does_nothing_on_real_navigation():
    driver = _FakeDriver(URL)
    raise_if_navigation_failed(driver, URL)  # must not raise


def test_fails_open_when_current_url_itself_raises():
    """A diagnostic-check failure must never mask or replace the real
    fetch outcome — today's behavior stays unchanged for that edge case."""
    driver = _RaisingCurrentUrlDriver()
    raise_if_navigation_failed(driver, URL)  # must not raise


def test_does_nothing_when_current_url_is_not_a_string():
    driver = _FakeDriver(current_url=None)  # type: ignore[arg-type]
    raise_if_navigation_failed(driver, URL)  # must not raise
