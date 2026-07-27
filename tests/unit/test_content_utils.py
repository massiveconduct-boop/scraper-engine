# tests/unit/test_content_utils.py
"""Shared fetch-content helpers: autoscroll (lazy-load / infinite-scroll)."""

import pytest

from fetcher._content_utils import autoscroll


class _MockPage:
    """Minimal page stub: evaluate() returns queued heights; scrollTo is a no-op."""

    def __init__(self, heights):
        self._heights = heights
        self._i = 0
        self.waits = 0

    async def evaluate(self, js):
        if "scrollTo" in js:
            return None
        h = self._heights[min(self._i, len(self._heights) - 1)]
        self._i += 1
        return h

    async def wait_for_timeout(self, ms):
        self.waits += 1


class TestAutoscroll:
    @pytest.mark.asyncio
    async def test_stops_after_consecutive_stable(self):
        # initial=100, 200, 300, 300, 300 → grows twice, then two flat passes
        # (default stable_passes_before_stop=2) → stops on the 4th pass.
        page = _MockPage([100, 200, 300, 300, 300])
        passes = await autoscroll(page, max_passes=8, wait_ms=1)
        assert passes == 4

    @pytest.mark.asyncio
    async def test_tolerates_ajax_lag(self):
        # Regression for the round-16 bug: a single flat pass (AJAX still in
        # flight) must NOT stop the loop if the next pass grows. Heights:
        # 100 →200(grow) →200(flat, in-flight) →300(grew!) →300 →300(2 flat→stop).
        page = _MockPage([100, 200, 200, 300, 300, 300])
        passes = await autoscroll(page, max_passes=8, wait_ms=1)
        # must reach the 300 batch, i.e. not stop at the transient flat on pass 2
        assert passes == 5

    @pytest.mark.asyncio
    async def test_respects_max_passes_cap(self):
        # height grows forever → capped at max_passes (prevents infinite feed loop)
        page = _MockPage([100, 200, 300, 400, 500, 600, 700, 800, 900])
        passes = await autoscroll(page, max_passes=4, wait_ms=1)
        assert passes == 4

    @pytest.mark.asyncio
    async def test_disabled_when_zero_passes(self):
        page = _MockPage([100, 200, 300])
        assert await autoscroll(page, max_passes=0, wait_ms=1) == 0
        assert page.waits == 0  # never scrolled

    @pytest.mark.asyncio
    async def test_never_raises_on_broken_page(self):
        class Broken:
            async def evaluate(self, js):
                raise RuntimeError("page gone")

        # a page that can't be evaluated just yields 0 passes, no exception
        assert await autoscroll(Broken(), max_passes=5, wait_ms=1) == 0
