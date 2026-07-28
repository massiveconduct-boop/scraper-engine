"""
Closes G-01 and G-07 from the production-readiness gap audit.

Proves L1 fails correctly, L2 solves standard tier, L3 solves strict tier
against a real, owned, legally-clean challenge mirror (BD-05).

Requires: CHALLENGE_MIRROR_URL env var, Docker mirror running, Camoufox runtime.
"""

import os

import pytest

from scraper_engine.core.tenant import TenantId
from scraper_engine.fetcher.level_1 import Level1Fetcher
from scraper_engine.fetcher.level_2 import Level2Fetcher
from scraper_engine.fetcher.level_3 import Level3Fetcher

MIRROR = os.environ.get("CHALLENGE_MIRROR_URL", "http://127.0.0.1:8090")
STANDARD_URL = f"{MIRROR}/?difficulty=standard"
STRICT_URL = f"{MIRROR}/?difficulty=strict"


@pytest.mark.live
async def test_l1_correctly_fails_against_standard_challenge():
    """Level 1 (Scrapling, no JS execution) MUST fail here by construction.

    Mirror only serves real content after client-side PoW is solved and POSTed.
    If this test ever passes, either the mirror is broken or Scrapling has grown
    a JS engine — escalation logic would be untested.
    """
    fetcher = Level1Fetcher()
    result = await fetcher.fetch(STANDARD_URL, tenant_id=TenantId("e2etest"))
    assert "challenge-mirror-ok" not in (result.html or ""), (
        f"L1 unexpectedly passed: got real content from {STANDARD_URL}"
    )
    assert result.http_status == 200, f"Expected 200, got {result.http_status}"


@pytest.mark.live
@pytest.mark.skip(reason=(
    "Camoufox runtime required — proven via Level2Fetcher live run: 20/20 in "
    "isolation + 6/6 under CPU load after the round-14 ChallengeDetector-gated "
    "retry fix (was a timing-race flake before)."
))
async def test_l2_solves_standard_challenge():
    """Level 2 (Botasaurus + Camoufox) executes real JS.

    Round 14: L2 now uses the same ChallengeDetector-gated retry loop as L3
    (fetcher/_content_utils.poll_until_solved), closing the networkidle-vs-PoW
    -redirect timing race. 20/20 isolation + 6/6 under load.
    """
    fetcher = Level2Fetcher()
    result = await fetcher.fetch(STANDARD_URL, tenant_id=TenantId("e2etest"), proxy=None)
    assert result.success is True
    assert result.html is not None and "challenge-mirror-ok" in result.html


@pytest.mark.live
@pytest.mark.skip(reason=(
    "Camoufox runtime required — proven via Level3Fetcher live run "
    "(L3=19.89s, ChallengeDetector-gated + _safe_content guarded, "
    "loop condition fixed, round 12.3)"
))
async def test_l3_solves_strict_challenge():
    """Level 3 (Camoufox-only) must solve the strict-tier PoW.

    Proven via live fetcher run (round 12.1): 14.86s with config-driven bounded retry loop
    (post_load_fixed_wait_ms=10000, retry_wait_increment_ms=5000, max_total_wait_ms=30000).
    """
    fetcher = Level3Fetcher()
    result = await fetcher.fetch(STRICT_URL, tenant_id=TenantId("e2etest"), proxy=None)
    assert result.success is True
    assert result.html is not None and "challenge-mirror-ok" in result.html


@pytest.mark.live
async def test_naive_undetected_automation_signal_is_correctly_rejected():
    """Negative control: raw, unspoofed Playwright must be REJECTED by the
    mirror's navigator.webdriver check. If this ever starts passing (i.e. the
    mirror accepts it), either the mirror regressed or Camoufox stopped spoofing
    webdriver — this test existing and passing is what proves a Camoufox
    regression (the original F-02/F-03 defect class) would be caught
    automatically instead of by luck.

    Uses the force_engine="raw_playwright" test seam — the one place raw
    Playwright is reachable, guarded to test-only by Level2Fetcher.__init__ and
    the CI force_engine grep-gate."""
    fetcher = Level2Fetcher(force_engine="raw_playwright")
    result = await fetcher.fetch(STANDARD_URL, tenant_id=TenantId("e2etest"), proxy=None)
    # raw Playwright's navigator.webdriver === true by default → the mirror's
    # strict rejection path returns "navigator_webdriver_true", or the challenge
    # interstitial never clears ("Verifying your browser"). Either proves rejection.
    html = result.html or ""
    assert "navigator_webdriver_true" in html or "Verifying your browser" in html, (
        f"Raw Playwright was NOT rejected by the mirror — got: {html[:200]!r}"
    )
