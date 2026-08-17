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

    def test_humanize_off_by_default_never_calls_move_mouse(self):
        driver = _driver_with_heights(10, 10, 10)
        botasaurus_autoscroll(driver, max_passes=5, wait_ms=1)
        driver.move_mouse_to_point.assert_not_called()

    def test_humanize_true_moves_mouse_before_each_pass(self):
        """Round 60 — humanize=True calls move_mouse_to_point() before each
        scroll pass (browser/_botasaurus_scroll.py::_humanize_mouse_before_pass),
        via a real (mocked) [innerWidth, innerHeight] run_js read."""
        driver = MagicMock()
        # initial height read, then per pass: viewport-size read, scrollTo,
        # height re-read — 2 passes total, flat immediately (stable at 10).
        driver.run_js.side_effect = [
            10,  # initial height
            [800, 600],  # pass1 viewport size
            None,  # pass1 scrollTo
            10,  # pass1 height (flat #1)
            [800, 600],  # pass2 viewport size
            None,  # pass2 scrollTo
            10,  # pass2 height (flat #2, stop)
        ]
        passes = botasaurus_autoscroll(driver, max_passes=5, wait_ms=1, humanize=True)
        assert passes == 2
        assert driver.move_mouse_to_point.call_count == 2

    def test_humanize_mouse_move_failure_does_not_break_scroll(self):
        """A broken move_mouse_to_point (e.g. no headless mouse-move support)
        must never take down the scroll loop it's meant to be decorating."""
        driver = MagicMock()
        driver.run_js.side_effect = [
            10,  # initial height
            [800, 600],  # pass1 viewport size
            None,  # pass1 scrollTo
            10,  # pass1 height (flat #1)
            [800, 600],  # pass2 viewport size
            None,  # pass2 scrollTo
            10,  # pass2 height (flat #2, stop)
        ]
        driver.move_mouse_to_point.side_effect = RuntimeError("no cursor")
        passes = botasaurus_autoscroll(driver, max_passes=5, wait_ms=1, humanize=True)
        assert passes == 2
