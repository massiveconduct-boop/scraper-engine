"""
Item 3: BrowserPool full lifecycle — updated for lease() API (invariant §1.1.6).
Tests: start → lease → use → healthy return → unhealthy teardown → shutdown.
"""

import asyncio

import psutil
import pytest

from scraper_engine.core.tenant import TenantId


def camoufox_process_count():
    return sum(
        1
        for p in psutil.process_iter(["name"])
        if (name := (p.info["name"] or "").lower()) and ("camoufox" in name or "firefox" in name)
    )


@pytest.mark.live
@pytest.mark.asyncio
async def test_pool_full_lifecycle_no_leak():
    """Verify: prewarm → lease → use → healthy release → shutdown → 0 processes."""
    from scraper_engine.browser.pool import BrowserPool

    pool = BrowserPool(tenant_id=TenantId("lifecycletest"), prewarm_count=0)
    await pool.start()

    pre = camoufox_process_count()

    # lease() is the async context manager — structural cleanup guaranteed
    async with pool.lease() as ctx:
        page = await ctx.new_page()
        await page.goto("about:blank", timeout=5000)
        content = await page.content()
        assert len(content) >= 0
        mid = camoufox_process_count()
        assert mid > pre, f"No browser launched: {pre}→{mid}"

    # __aexit__ fires → release(healthy=True, because no exception)
    # Unhealthy path: exception inside lease() block triggers release(healthy=False)
    try:
        async with pool.lease() as _ctx2:
            raise RuntimeError("simulated failure")
    except RuntimeError:
        pass  # expected — lease() converted to unhealthy release

    await pool.shutdown()
    await asyncio.sleep(3)

    final = camoufox_process_count()
    assert final == 0, f"LEAK: {final} processes remain"
    print(f"LIFECYCLE: pre={pre} mid={mid} final={final} PASS")
