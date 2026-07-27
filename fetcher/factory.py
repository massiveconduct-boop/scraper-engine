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

from fetcher.challenge_detector import ChallengeDetector
from fetcher.level_1 import Level1Fetcher
from fetcher.level_2 import Level2Fetcher
from fetcher.level_3 import Level3Fetcher

if TYPE_CHECKING:
    from config.schema import AppConfig


def build_level1_fetcher(config: AppConfig) -> Level1Fetcher:
    """Construct the L1 (HTTP/Scrapling) fetcher. Takes no wait-strategy config
    today, but centralised here so every production call site has one path."""
    return Level1Fetcher()


def build_level2_fetcher(
    config: AppConfig, challenge_detector: ChallengeDetector | None = None
) -> Level2Fetcher:
    """Construct the L2 (Botasaurus+Camoufox) fetcher from config.levels.level_2."""
    lvl = config.levels.level_2
    return Level2Fetcher(
        goto_wait_until=lvl.goto_wait_until,
        networkidle_timeout_ms=lvl.networkidle_timeout_ms,
        max_total_wait_ms=lvl.max_total_wait_ms,
        retry_wait_increment_ms=lvl.retry_wait_increment_ms,
        scroll_passes=lvl.scroll_passes,
        scroll_wait_ms=lvl.scroll_wait_ms,
        challenge_detector=challenge_detector or ChallengeDetector(),
    )


def build_level3_fetcher(
    config: AppConfig, challenge_detector: ChallengeDetector | None = None
) -> Level3Fetcher:
    """Construct the L3 (Camoufox-only) fetcher from config.levels.level_3."""
    lvl = config.levels.level_3
    return Level3Fetcher(
        goto_wait_until=lvl.goto_wait_until,
        post_load_fixed_wait_ms=lvl.post_load_fixed_wait_ms,
        max_total_wait_ms=lvl.max_total_wait_ms,
        retry_wait_increment_ms=lvl.retry_wait_increment_ms,
        scroll_passes=lvl.scroll_passes,
        scroll_wait_ms=lvl.scroll_wait_ms,
        challenge_detector=challenge_detector or ChallengeDetector(),
    )
