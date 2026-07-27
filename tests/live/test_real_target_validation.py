"""Real-target validation (round 13 Part C).

Every escalation-ladder proof in this project has been against the self-hosted
challenge mirror — the right call for iterating safely and legally, but it leaves
one real question open: does this system work against a real *commercial*
anti-bot product, not just a fair, controllable stand-in?

This test answers that ONLY against infrastructure you own or explicitly control
(recommended: Cloudflare free-tier bot-fight mode on a domain you own — real
challenge logic, zero ToS ambiguity because it's your property). It is gated
behind an explicit opt-in env var so it never runs by accident in CI or against
the wrong host.

DO NOT point REAL_TARGET_VALIDATION_URL at a third party's production site
without their explicit permission, regardless of how "low-stakes" it seems —
the one hard line here, consistent with the original challenge-mirror decision.

This is deliberately EXPLORATORY, not pass/fail against a pre-written assertion.
Its job is to OBSERVE and RECORD real behavior — the first test in this project
where "it doesn't fully work yet" is an expected, useful, non-blocking outcome.
The point is to find where the real gap is (timeout budget wrong for a real
product? ChallengeDetector's pattern list too narrow for Cloudflare's real
interstitial?), not to prove there isn't one.

Enable with:
    REAL_TARGET_VALIDATION_ENABLED=true \
    REAL_TARGET_VALIDATION_URL=https://your-own-cloudflare-protected-domain.example \
    .venv/bin/pytest tests/live/test_real_target_validation.py -v -s
"""

import os

import pytest

from config.loader import load_config
from core.tenant import TenantId
from fetcher.factory import (
    build_level1_fetcher,
    build_level2_fetcher,
    build_level3_fetcher,
)

REAL_TARGET_URL = os.environ.get("REAL_TARGET_VALIDATION_URL")
REAL_TARGET_ENABLED = os.environ.get("REAL_TARGET_VALIDATION_ENABLED") == "true"

pytestmark = pytest.mark.skipif(
    not REAL_TARGET_ENABLED or not REAL_TARGET_URL,
    reason=(
        "Real-target validation requires REAL_TARGET_VALIDATION_ENABLED=true and "
        "REAL_TARGET_VALIDATION_URL pointing at infrastructure YOU OWN. Never "
        "enable this against a third party's site without permission."
    ),
)

_TENANT = TenantId("realtargettest")


@pytest.mark.live
async def test_l1_against_real_cloudflare_challenge():
    """L1 (HTTP, no JS) against a real anti-bot product. Expected to FAIL/return
    a challenge page — that failure is what should trigger escalation. Observed,
    not asserted."""
    config = load_config()
    fetcher = build_level1_fetcher(config)
    result = await fetcher.fetch(REAL_TARGET_URL, _TENANT)
    print(
        f"\nL1 vs real target: success={result.success} "
        f"is_challenge_page={result.is_challenge_page} "
        f"http_status={result.http_status} category={result.failure_category}"
    )
    if result.html:
        print(f"L1 html head: {result.html[:300]!r}")


@pytest.mark.live
async def test_l2_l3_against_real_cloudflare_challenge():
    """L2 then (if L2 fails) L3 against a real anti-bot product. Records real
    timings, real failure_category, and — critically — the captured HTML if it
    fails, so we can see whether the timeout budget or ChallengeDetector's
    pattern list needs adjusting for a real product vs. the mirror."""
    config = load_config()
    l2 = build_level2_fetcher(config)
    l3 = build_level3_fetcher(config)

    r2 = await l2.fetch(REAL_TARGET_URL, _TENANT, proxy=None)
    print(
        f"\nL2 vs real target: success={r2.success} duration_ms={r2.duration_ms} "
        f"category={r2.failure_category}"
    )
    if not r2.success or (r2.html and "challenge" in r2.html.lower()):
        if r2.html:
            print(f"L2 captured html head: {r2.html[:500]!r}")
        r3 = await l3.fetch(REAL_TARGET_URL, _TENANT, proxy=None)
        print(
            f"L3 vs real target: success={r3.success} duration_ms={r3.duration_ms} "
            f"category={r3.failure_category}"
        )
        if r3.html:
            print(f"L3 captured html head: {r3.html[:500]!r}")
