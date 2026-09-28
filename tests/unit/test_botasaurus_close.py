"""browser/_botasaurus_close.py — a Driver whose own close() fails partway
must not leave Chrome or its Xvfb display running.

Live finding (2026-09-28): with tiny_profile on, Driver.close() calls
save_cookies() first; when Chrome has died that raises
"[Errno 111] Connection refused" and the browser, display and local proxy
are never closed. Workers had 547 chromium and 120 Xvfb processes.
"""

from unittest.mock import MagicMock

from scraper_engine.browser import _botasaurus_close
from scraper_engine.browser._botasaurus_close import close_driver, finish_close


def _driver(*, close_error=None, process_alive=True, display_alive=True):
    driver = MagicMock()
    process = MagicMock() if process_alive else None
    driver._browser._process = process
    driver.config._display.is_alive.return_value = display_alive
    if close_error is not None:
        driver.close.side_effect = close_error
    return driver, process


def test_close_that_fails_before_closing_the_browser_kills_chrome_and_xvfb(caplog):
    driver, process = _driver(close_error=Exception("[Errno 111] Connection refused"))
    close_driver(driver)
    process.kill.assert_called_once()
    process.wait.assert_called_once_with(timeout=_botasaurus_close._PROCESS_WAIT_SECONDS)
    assert driver._browser._process is None
    # config.close() stops the display and the local auth proxy together.
    driver.config.close.assert_called_once()
    assert "botasaurus_close_failed" in caplog.text


def test_close_that_worked_leaves_nothing_to_kill():
    driver, _ = _driver(process_alive=False, display_alive=False)
    close_driver(driver)
    driver.close.assert_called_once()
    driver.config.close.assert_not_called()
    driver.config._display.stop.assert_not_called()


def test_display_still_up_after_chrome_is_gone_is_stopped():
    """Browser.close() cleared _process but failed before config.close()."""
    driver, _ = _driver(process_alive=False, display_alive=True)
    finish_close(driver)
    driver.config.close.assert_not_called()
    driver.config._display.stop.assert_called_once()


def test_every_cleanup_step_runs_even_when_the_ones_before_it_fail(monkeypatch):
    driver, process = _driver()
    process.kill.side_effect = ProcessLookupError()
    process.wait.side_effect = OSError()
    driver.config.close.side_effect = RuntimeError("proxy-chain gone")
    driver.config._display.stop.side_effect = RuntimeError("already stopped")
    cleanup = MagicMock(side_effect=OSError())
    monkeypatch.setattr(_botasaurus_close, "cleanup_stale_display", cleanup)
    finish_close(driver)  # never raises
    driver.config.close.assert_called_once()
    driver.config._display.stop.assert_called_once()
    cleanup.assert_called_once_with(driver)


def test_a_driver_without_the_private_attributes_is_left_alone():
    finish_close(object())
