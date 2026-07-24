"""
Item 3 (round 6): BrowserPool full lifecycle live test.

pool.start() creates CamoufoxWrapper objects (lazy — browser launches
on __aenter__). This test verifies the full lifecycle:
  start → acquire → launch → use → healthy release → acquire again →
  unhealthy release → shutdown → zero processes remain.
"""

import asyncio

import psutil
import pytest

from core.tenant import TenantId


def camoufox_process_count():
    return sum(
        1 for p in psutil.process_iter(["name"])
        if (name := (p.info["name"] or "").lower())
        and ("camoufox" in name or "firefox" in name)
    )


@pytest.mark.live
@pytest.mark.asyncio
async def test_pool_full_lifecycle_no_leak():
    from browser.pool import BrowserPool

    pool = BrowserPool(tenant_id=TenantId("lifecycletest"), prewarm_count=0)
    await pool.start()

    pre_count = camoufox_process_count()

    # Acquire → launch (browser process starts here)
    wrapper = await pool.acquire(proxy=None)
    async with wrapper as ctx:
        page = await ctx.new_page()
        await page.goto("http://httpbin.org/ip", timeout=15000)
        content = await page.content()
        assert len(content) > 0, "page content empty"

    active_after_use = camoufox_process_count()
    assert active_after_use > pre_count, (
        f"No browser process after acquire+use: was {pre_count}, now {active_after_use}"
    )

    # Healthy release — wrapper returned to pool
    await pool.release(wrapper, healthy=True)

    # Acquire again — different wrapper (should still work)
    wrapper2 = await pool.acquire(proxy=None)
    async with wrapper2 as ctx2:
        page2 = await ctx2.new_page()
        await page2.goto("http://httpbin.org/ip", timeout=15000)

    # Unhealthy release — must NOT return to pool, must NOT leak
    await pool.release(wrapper2, healthy=False)

    await pool.shutdown()
    await asyncio.sleep(3)

    final = camoufox_process_count()
    assert final == 0, f"LEAK: {final} camoufox/firefox processes still running"

    print(
        f"BrowserPool lifecycle PASS: "
        f"pre={pre_count}, active={active_after_use}, final={final}"
    )
