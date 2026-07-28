# fetcher/factory.py
"""Single source of truth for constructing fetchers from AppConfig.

Nothing outside this module should call Level1Fetcher()/Level2Fetcher()/
Level3Fetcher() directly with hand-picked kwargs — that's exactly the drift
risk this module exists to close (round 13 A1). A new production call site that
forgets to wire config would silently fall back to constructor defaults with no
error; routing all construction through here makes production.yaml authoritative
everywhere.

Enforced by the "no direct fetcher construction outside factory" CI gate in
.github/workflows/test.yml. Tests are exempt — a unit test constructing a
fetcher directly with mock args is normal; it's production call sites that must
go through the factory.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from core.ssrf_guard import SSRFGuard
from fetcher.challenge_detector import ChallengeDetector
from fetcher.level_1 import Level1Fetcher
from fetcher.level_2 import Level2Fetcher
from fetcher.level_3 import Level3Fetcher

if TYPE_CHECKING:
    from browser.botasaurus_pool import BotasaurusPool
    from browser.pool import BrowserPool
    from config.schema import AppConfig
    from services.captcha_solver import CaptchaSolver


def _build_ssrf_guard(config: AppConfig) -> SSRFGuard:
    """One shared guard per fetcher build, honoring ssrf_guard.additional_denied_cidrs
    — without this, every fetcher's `ssrf_guard or SSRFGuard()` fallback silently
    used the zero-arg default and the config field never took effect."""
    return SSRFGuard(config.ssrf_guard.additional_denied_cidrs)


def build_level1_fetcher(config: AppConfig) -> Level1Fetcher:
    """Construct the L1 (HTTP/Scrapling) fetcher. Takes no wait-strategy config
    today, but centralised here so every production call site has one path.

    Threads an optional FirecrawlClient (env-gated on FIRECRAWL_API_KEY, same
    pattern as captcha_solver) for markdown conversion — None disables it.

    Threads an optional BotasaurusRequestsClient (round 26), gated on
    config.botasaurus.l1_ja3_client_enabled (default off — a brand-new code
    path with no live-traffic validation yet). None disables it, same
    build-or-None pattern as the firecrawl client above."""
    from services.botasaurus_requests_client import build_ja3_client
    from services.firecrawl_client import build_firecrawl_client

    return Level1Fetcher(
        firecrawl_client=build_firecrawl_client(),
        ssrf_guard=_build_ssrf_guard(config),
        ja3_client=build_ja3_client(
            config.botasaurus.l1_ja3_client_enabled,
            timeout_seconds=float(config.levels.level_1.timeout_seconds),
        ),
    )


def build_level2_fetcher(
    config: AppConfig,
    challenge_detector: ChallengeDetector | None = None,
    captcha_solver: CaptchaSolver | None = None,
    pool: BrowserPool | None = None,
    botasaurus_pool: BotasaurusPool | None = None,
) -> Level2Fetcher:
    """Construct the L2 (Botasaurus+Camoufox) fetcher from config.levels.level_2.

    captcha_solver is optional — the worker builds it once (env keys + budget)
    and threads it through so an in-page CAPTCHA can be solved mid-fetch. None
    disables solving (fetch still runs, challenges just aren't token-solved).

    pool is optional (round 25) — when provided, every fetch leases a hot
    browser from it instead of cold-starting a fresh CamoufoxWrapper.

    botasaurus_pool is optional (round 26) — when provided, a 2nd+ Botasaurus
    fetch for the same proxy+domain within the job reuses the live driver
    (see browser/botasaurus_pool.py) instead of relaunching Botasaurus.

    Botasaurus is only constructed when lvl.engine actually says so (round
    25) — Level2Fetcher falls straight back to Camoufox when it's None, so
    an engine value of "camoufox" (no Botasaurus attempt) stays a real,
    supported configuration, not just a historical artifact."""
    from fetcher.botasaurus_wrapper import BotasaurusWrapper

    lvl = config.levels.level_2
    bconf = config.botasaurus
    botasaurus = (
        BotasaurusWrapper(
            bypass_cloudflare=bconf.bypass_cloudflare,
            tiny_profile=bconf.tiny_profile,
            remove_default_browser_check_argument=bconf.remove_default_browser_check_argument,
            close_on_crash=bconf.close_on_crash,
            use_random_sleep=bconf.random_sleep_enabled,
            hashed_fingerprint=bconf.hashed_fingerprint,
            max_retry=bconf.max_retry,
        )
        if "botasaurus" in lvl.engine
        else None
    )
    return Level2Fetcher(
        goto_wait_until=lvl.goto_wait_until,
        networkidle_timeout_ms=lvl.networkidle_timeout_ms,
        max_total_wait_ms=lvl.max_total_wait_ms,
        retry_wait_increment_ms=lvl.retry_wait_increment_ms,
        scroll_passes=lvl.scroll_passes,
        scroll_wait_ms=lvl.scroll_wait_ms,
        challenge_detector=challenge_detector or ChallengeDetector(),
        captcha_solver=captcha_solver if lvl.capsolver_enabled else None,
        ssrf_guard=_build_ssrf_guard(config),
        pool=pool,
        botasaurus=botasaurus,
        botasaurus_pool=botasaurus_pool,
    )


def build_level3_fetcher(
    config: AppConfig,
    challenge_detector: ChallengeDetector | None = None,
    captcha_solver: CaptchaSolver | None = None,
    pool: BrowserPool | None = None,
) -> Level3Fetcher:
    """Construct the L3 (Camoufox-only) fetcher from config.levels.level_3.

    captcha_solver is threaded through the same way as for L2 (see above).
    pool is optional (round 25), same hot-browser-lease behavior as L2."""
    lvl = config.levels.level_3
    return Level3Fetcher(
        goto_wait_until=lvl.goto_wait_until,
        post_load_fixed_wait_ms=lvl.post_load_fixed_wait_ms,
        max_total_wait_ms=lvl.max_total_wait_ms,
        retry_wait_increment_ms=lvl.retry_wait_increment_ms,
        scroll_passes=lvl.scroll_passes,
        scroll_wait_ms=lvl.scroll_wait_ms,
        challenge_detector=challenge_detector or ChallengeDetector(),
        captcha_solver=captcha_solver if lvl.capsolver_enabled else None,
        ssrf_guard=_build_ssrf_guard(config),
        pool=pool,
    )
