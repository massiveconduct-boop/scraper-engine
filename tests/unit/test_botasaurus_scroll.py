# tests/unit/test_botasaurus_scroll.py
"""Round 58: browser/_botasaurus_scroll.py::botasaurus_autoscroll is a sync
port of fetcher/_content_utils.py::autoscroll's height-stability algorithm
for Botasaurus's Selenium-style Driver (driver.run_js, no awaitable
page.evaluate/wait_for_timeout). No real Chrome/botasaurus driver launches
here — driver.run_js is mocked to return a scripted sequence of heights."""

from unittest.mock import MagicMock

from scraper_engine.browser._botasaurus_scroll import botasaurus_autoscroll


def _driver_with_heights(*heights: int) -> MagicMock:
    """Each pass makes two run_js calls: the scrollTo command (return value
    ignored) and the height re-read. The initial height read (before the
    loop starts) consumes the first value in `heights`; each subsequent pair
    of run_js calls consumes: (scrollTo -> None, height-read -> next value)."""
    driver = MagicMock()
    side_effect: list[object] = [heights[0]]
    for h in heights[1:]:
        side_effect.append(None)  # scrollTo call, return value unused
        side_effect.append(h)  # height re-read
    driver.run_js.side_effect = side_effect
    return driver


class TestBotasaurusAutoscroll:
    def test_max_passes_non_positive_returns_zero_without_calling_run_js(self):
        driver = MagicMock()
        assert botasaurus_autoscroll(driver, max_passes=0, wait_ms=1) == 0
        driver.run_js.assert_not_called()

    def test_initial_height_read_failure_returns_zero(self):
        driver = MagicMock()
        driver.run_js.side_effect = RuntimeError("no page")
        assert botasaurus_autoscroll(driver, max_passes=5, wait_ms=1) == 0

    def test_growing_then_flat_stops_after_two_consecutive_stable_passes(self):
        # initial=10, pass1 -> 20 (growth), pass2 -> 30 (growth),
        # pass3 -> 30 (flat #1), pass4 -> 30 (flat #2, stop)
        driver = _driver_with_heights(10, 20, 30, 30, 30)
        passes = botasaurus_autoscroll(driver, max_passes=10, wait_ms=1)
        assert passes == 4

    def test_max_passes_caps_when_height_keeps_growing(self):
        driver = _driver_with_heights(10, 20, 30, 40)
        passes = botasaurus_autoscroll(driver, max_passes=3, wait_ms=1)
        assert passes == 3

    def test_exception_mid_loop_breaks_and_returns_passes_so_far(self):
        driver = MagicMock()
        # initial height, then pass1 scrollTo ok, pass1 height-read raises
        driver.run_js.side_effect = [10, None, RuntimeError("crashed")]
        passes = botasaurus_autoscroll(driver, max_passes=5, wait_ms=1)
        assert passes == 0

    def test_never_raises_even_when_driver_is_totally_broken(self):
        driver = MagicMock()
        driver.run_js.side_effect = RuntimeError("boom")
        botasaurus_autoscroll(driver, max_passes=5, wait_ms=1)  # must not raise
