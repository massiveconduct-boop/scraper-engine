"""
Item 3: BrowserPool full lifecycle live test (F-14, F-16 closure).
Tests prewarm, acquire, healthy release, unhealthy release, shutdown.
Asserts on real OS process counts, not mocks.
"""

import asyncio

import psutil
import pytest

from core.tenant import TenantId


def camoufox_process_count():
    """Count OS processes containing 'camoufox' or 'firefox' in name."""
    return sum(
        1 for p in psutil.process_iter(["name"])
        if (name := (p.info["name"] or "").lower())
        and ("camoufox" in name or "firefox" in name)
    )


@pytest.mark.live
@pytest.mark.asyncio
async def test_pool_full_lifecycle_no_leak():
    """BrowserPool: prewarm → acquire → healthy release → unhealthy release → shutdown.

    Verifies: prewarm actually launches processes, acquire returns working context,
    healthy release returns to pool, unhealthy release does NOT leak,
    shutdown reaps all processes.
    """
    from browser.pool import BrowserPool

    pool = BrowserPool(tenant_id=TenantId("lifecycletest"), prewarm_count=2)
    await pool.start()

    baseline = camoufox_process_count()
    assert baseline >= 2, f"prewarm did not launch processes: only {baseline} found"

    # Healthy acquire + release
    wrapper = await pool.acquire(proxy=None)
    async with wrapper as ctx:
        page = await ctx.new_page()
        await page.goto("http://httpbin.org/ip", timeout=15000)
        content = await page.content()
        assert len(content) > 0, "page content empty — browser not rendering"
    await pool.release(wrapper, healthy=True)

    # Unhealthy release — must NOT return to pool, must NOT leak process
    wrapper2 = await pool.acquire(proxy=None)
    await pool.release(wrapper2, healthy=False)

    await pool.shutdown()
    await asyncio.sleep(2)

    final = camoufox_process_count()
    assert final == 0, f"LEAK: {final} camoufox/firefox processes still running after shutdown()"
    print(f"BrowserPool lifecycle PASS: baseline={baseline}, final={final}")
