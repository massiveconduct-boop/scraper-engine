"""
Chaos test: prove _safe_content survives mid-poll navigation races.

These are real-browser integration tests — they use CamoufoxWrapper to launch
an actual Firefox process, navigate to the challenge mirror, and then trigger
a concurrent reload while calling _safe_content.  The reload introduces a
genuine browser-level navigation event that races with page.content().

Two variants:
  Test 1 — 200ms delay between triggering reload and reading content
  Test 2 — zero delay (aggressive race)

Both tests prove the guard does not crash.  Whether the race is "won" by the
reload or by the content() call depends on browser timing; either outcome
(reload completed → real content, or mid-navigation → None) is acceptable —
the point is that _safe_content never throws.
"""

import asyncio
import os

import pytest

from core.tenant import TenantId
from fetcher._content_utils import safe_content

MIRROR = os.environ.get("CHALLENGE_MIRROR_URL", "http://127.0.0.1:8090")


def _camoufox_installed() -> bool:
    """True only if the Camoufox browser binary is actually fetched. The pip
    package is present in CI but the ~300MB browser is not (`camoufox fetch`),
    so these real-browser races must skip there — same "run locally, skip in CI"
    convention as the other Camoufox-dependent tests."""
    try:
        from camoufox.pkgman import installed_verstr

        return bool(installed_verstr())
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _camoufox_installed(),
    reason="Camoufox browser binary not installed (run `camoufox fetch`); skipped in CI",
)


async def test_mid_poll_reload_with_delay() -> None:
    """Trigger reload, wait 200ms, then call _safe_content.

    The 200ms gives the browser time to start navigating.  If the reload is
    in-flight, _safe_content returns None.  If it already completed, the
    returned string length is printed.
    """
    from browser.camoufox_wrapper import CamoufoxWrapper

    wrapper = CamoufoxWrapper(proxy=None, tenant_id=TenantId("chaostest"))
    async with wrapper as browser_context:
        page = await browser_context.new_page()
        await page.goto(
            f"{MIRROR}/?difficulty=strict", wait_until="load", timeout=60000
        )
        await page.wait_for_timeout(3000)

        # Fire reload mid-PoW-solve, then race _safe_content
        asyncio.create_task(page.evaluate("() => window.location.reload()"))
        await asyncio.sleep(0.2)
        result = await safe_content(page)

        if result is None:
            print(
                "GUARD HELD: _safe_content returned None during mid-poll "
                "navigation — loop keeps polling"
            )
        else:
            print(
                f"GUARD OK: _safe_content returned {len(result)} chars "
                f"(reload may have completed) — no crash"
            )

    print("TEST 1 COMPLETE — no crash, guard functioned")


async def test_mid_poll_reload_aggressive_race() -> None:
    """Trigger reload and IMMEDIATELY call _safe_content — zero delay.

    This is the most aggressive race we can construct: the reload is fired
    concurrently and content() is called without waiting, maximising the
    chance that the page is mid-navigation.
    """
    from browser.camoufox_wrapper import CamoufoxWrapper

    wrapper = CamoufoxWrapper(proxy=None, tenant_id=TenantId("chaostest"))
    async with wrapper as browser_context:
        page = await browser_context.new_page()
        await page.goto(
            f"{MIRROR}/?difficulty=strict", wait_until="load", timeout=60000
        )
        await page.wait_for_timeout(5000)

        # Fire reload then IMMEDIATELY read content
        asyncio.create_task(page.evaluate("() => window.location.reload()"))
        result = await safe_content(page)

        if result is None:
            print(
                "AGGRESSIVE RACE: GUARD HELD — _safe_content returned None "
                "(page mid-navigation)"
            )
        else:
            print(
                f"AGGRESSIVE RACE: _safe_content returned {len(result)} chars "
                f"— reload completed before content() call, no crash"
            )

    print("TEST 2 COMPLETE — no crash, guard functioned")


async def main() -> None:
    await test_mid_poll_reload_with_delay()
    await test_mid_poll_reload_aggressive_race()


if __name__ == "__main__":
    asyncio.run(main())
