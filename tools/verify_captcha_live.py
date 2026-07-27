#!/usr/bin/env python
"""Live end-to-end verification of the round-20 CAPTCHA fetch-path wiring.

Runs the REAL fetcher/_captcha.solve_captcha_on_page against a REAL Camoufox
browser loading a REAL reCAPTCHA v2 page (Google's demo). Exercises every stage
that was previously only fake-page tested:

  1. DOM detection    — read {kind, sitekey} from the live reCAPTCHA widget
  2. provider solve    — real NoCaptchaAI createTask/poll (may return None if the
                         account's reCAPTCHA capability is inactive — round 19)
  3. token injection   — inject into the live DOM textarea; read it back

Then, independently of the provider, it force-injects a marker token to prove
the injection JS mutates the real DOM even when the provider can't supply a
token (isolates the browser-side half from the account-entitlement blocker).

Keys are read from the process environment (loaded from .env by the caller);
never printed. Usage:
  set -a && . ./.env && set +a && .venv/bin/python tools/verify_captcha_live.py
"""

from __future__ import annotations

import asyncio
import os
from unittest.mock import AsyncMock

from camoufox.async_api import AsyncCamoufox

from core.tenant import TenantId
from fetcher._captcha import (
    _DETECT_JS,
    _INJECT_RECAPTCHA_JS,
    solve_captcha_on_page,
)
from services.captcha_solver import CaptchaSolver

DEMO_URL = "https://www.google.com/recaptcha/api2/demo"
TENANT = TenantId("livecheck")


def _mask(v: str | None) -> str:
    if not v:
        return "<absent>"
    return f"{v[:4]}…{v[-2:]} (len={len(v)})"


async def main() -> None:
    nk = os.environ.get("NOCAPTCHA_AI_API_KEY")
    ck = os.environ.get("CAPSOLVER_API_KEY")
    print(f"NOCAPTCHA_AI_API_KEY: {_mask(nk)}")
    print(f"CAPSOLVER_API_KEY:    {_mask(ck)}")

    # Real provider clients with a budget stub that always permits (budget gating
    # is unit-tested separately; here we want to reach the live provider).
    budget = AsyncMock()
    budget.check_and_reserve.return_value = True
    from services.capsolver import CapSolverClient
    from services.nocaptcha import NoCaptchaAIClient

    primary = NoCaptchaAIClient(nk, budget) if nk else None
    fallback = CapSolverClient(ck, budget) if ck else None
    assert primary is not None, "NOCAPTCHA_AI_API_KEY required for a live run"
    solver = CaptchaSolver(primary, fallback)

    print(f"\nLaunching Camoufox (headless) → {DEMO_URL}")
    async with AsyncCamoufox(headless=True) as browser:
        page = await browser.new_page()
        await page.goto(DEMO_URL, wait_until="load", timeout=60_000)
        await page.wait_for_timeout(3000)

        # STAGE 1 — live DOM detection (the fake-page-only half, now real)
        detected = await page.evaluate(_DETECT_JS)
        print(f"\n[1] DOM detect  → {detected}")
        if not detected or not detected.get("sitekey"):
            print("    FAIL: no widget/sitekey extracted from live DOM")
            return
        print(f"    OK: kind={detected['kind']} sitekey={detected['sitekey']}")

        # STAGE 2+3 — full production path: real solve + real inject
        print("\n[2+3] solve_captcha_on_page (real provider + real inject)…")
        solved = await solve_captcha_on_page(
            page, solver=solver, tenant_id=TENANT, url=DEMO_URL
        )
        textarea = await page.evaluate(
            "() => (document.getElementById('g-recaptcha-response')||{}).value || ''"
        )
        print(f"    solve_captcha_on_page returned: {solved}")
        print(f"    #g-recaptcha-response length after: {len(textarea)}")

        # INDEPENDENT — prove the injection JS mutates the real DOM regardless of
        # whether the provider produced a token (isolates the account blocker).
        print("\n[inject-only] force a marker token into the live DOM…")
        marker = "LIVE_MARKER_TOKEN_0123456789"
        await page.evaluate(_INJECT_RECAPTCHA_JS, marker)
        got = await page.evaluate(
            "() => (document.getElementById('g-recaptcha-response')||{}).value || ''"
        )
        print(f"    textarea == marker: {got == marker} (got len={len(got)})")

    provider = "PASS (token obtained)" if solved else "NO TOKEN (see provider output above)"
    print("\n=== summary ===")
    print(f"live DOM detection: {'PASS' if detected.get('sitekey') else 'FAIL'}")
    print(f"live injection:     {'PASS' if got == marker else 'FAIL'}")
    print(f"live provider solve: {provider}")


if __name__ == "__main__":
    asyncio.run(main())
