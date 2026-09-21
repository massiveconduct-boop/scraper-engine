# tests/unit/test_level_memory.py
"""Round 63: LevelMemory — the per-domain "start the ladder here" hint.

The hint is the fix for an external consumer's measurement that ~84% of a
Jumia job's wall time was the L1/L2 attempts that had already failed for that
domain dozens of times. These tests pin the two properties that make it safe
to have on by default: it only ever skips levels, and it re-probes.
"""

from unittest.mock import AsyncMock

import pytest

from scraper_engine.config.schema import EscalationConfig
from scraper_engine.core.tenant import TenantId
from scraper_engine.orchestrator.level_memory import LevelMemory

TENANT = TenantId("levelmem")
LEVELS = [1, 2, 3]


def _memory(config: EscalationConfig | None = None, *, hint: str | None = None, count: int = 1):
    redis = AsyncMock()
    redis.raw.get.return_value = hint
    redis.raw.incr.return_value = count
    return LevelMemory(redis, config or EscalationConfig()), redis


class TestStartLevel:
    @pytest.mark.asyncio
    async def test_no_hint_starts_at_the_bottom(self):
        memory, _ = _memory(hint=None)
        assert await memory.start_level(TENANT, "a.example", LEVELS) == 1

    @pytest.mark.asyncio
    async def test_hint_skips_the_levels_that_already_failed(self):
        memory, _ = _memory(hint="3")
        assert await memory.start_level(TENANT, "a.example", LEVELS) == 3

    @pytest.mark.asyncio
    async def test_disabled_never_touches_redis(self):
        memory, redis = _memory(EscalationConfig(level_memory_enabled=False), hint="3")
        assert await memory.start_level(TENANT, "a.example", LEVELS) == 1
        redis.raw.get.assert_not_called()

    @pytest.mark.asyncio
    async def test_hint_is_clamped_into_a_caller_narrowed_ladder(self):
        """max_level=2 narrows LEVELS to [1, 2]; an L3 hint must become 2,
        not silently start a level the caller excluded."""
        memory, _ = _memory(hint="3")
        assert await memory.start_level(TENANT, "a.example", [1, 2]) == 2

    @pytest.mark.asyncio
    async def test_hint_below_the_caller_floor_is_clamped_up(self):
        """min_level=2 narrows LEVELS to [2, 3]; a stale L1-era hint must not
        drag the start back below what the caller explicitly asked for."""
        memory, _ = _memory(hint="1")
        assert await memory.start_level(TENANT, "a.example", [2, 3]) == 2

    @pytest.mark.asyncio
    async def test_garbage_hint_falls_back_to_the_bottom(self):
        memory, _ = _memory(hint="not-a-level")
        assert await memory.start_level(TENANT, "a.example", LEVELS) == 1

    @pytest.mark.asyncio
    async def test_redis_failure_costs_latency_not_correctness(self):
        memory, redis = _memory(hint="3")
        redis.raw.get.side_effect = ConnectionError("redis down")
        assert await memory.start_level(TENANT, "a.example", LEVELS) == 1


class TestReprobe:
    @pytest.mark.asyncio
    async def test_every_nth_url_ignores_the_hint(self):
        """The TTL alone cannot rediscover a target that got easier — a
        continuously crawled domain refreshes its hint before it can expire.
        The counter is what makes the memory not a one-way ratchet."""
        memory, _ = _memory(EscalationConfig(reprobe_every=5), hint="3", count=5)
        assert await memory.start_level(TENANT, "a.example", LEVELS) == 1

    @pytest.mark.asyncio
    async def test_non_nth_url_uses_the_hint(self):
        memory, _ = _memory(EscalationConfig(reprobe_every=5), hint="3", count=4)
        assert await memory.start_level(TENANT, "a.example", LEVELS) == 3

    @pytest.mark.asyncio
    async def test_first_url_for_a_domain_gives_the_counter_a_ttl(self):
        memory, redis = _memory(EscalationConfig(reprobe_every=5), hint="3", count=1)
        await memory.start_level(TENANT, "a.example", LEVELS)
        redis.raw.expire.assert_awaited_once()
        assert redis.raw.expire.await_args.args[1] == 86400

    @pytest.mark.asyncio
    async def test_later_urls_do_not_re_arm_the_ttl(self):
        memory, redis = _memory(EscalationConfig(reprobe_every=5), hint="3", count=2)
        await memory.start_level(TENANT, "a.example", LEVELS)
        redis.raw.expire.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_reprobe_every_zero_disables_reprobing(self):
        memory, redis = _memory(EscalationConfig(reprobe_every=0), hint="3", count=99)
        assert await memory.start_level(TENANT, "a.example", LEVELS) == 3
        redis.raw.incr.assert_not_called()


class TestRecordSuccess:
    @pytest.mark.asyncio
    async def test_records_a_browser_level_with_the_configured_ttl(self):
        memory, redis = _memory()
        await memory.record_success(TENANT, "a.example", 3)
        redis.raw.set.assert_awaited_once_with("levelhint:levelmem:a.example", "3", ex=86400)

    @pytest.mark.asyncio
    async def test_level_1_deletes_rather_than_records(self):
        """L1 is where the ladder already starts, so "start at 1" carries no
        information — and writing it would leave a stale higher hint alive.
        Deleting is how a re-probe that succeeds lower actually takes effect."""
        memory, redis = _memory()
        await memory.record_success(TENANT, "a.example", 1)
        redis.raw.delete.assert_awaited_once_with("levelhint:levelmem:a.example")
        redis.raw.set.assert_not_called()

    @pytest.mark.asyncio
    async def test_disabled_never_writes(self):
        memory, redis = _memory(EscalationConfig(level_memory_enabled=False))
        await memory.record_success(TENANT, "a.example", 3)
        redis.raw.set.assert_not_called()
        redis.raw.delete.assert_not_called()

    @pytest.mark.asyncio
    async def test_redis_failure_is_swallowed(self):
        memory, redis = _memory()
        redis.raw.set.side_effect = ConnectionError("redis down")
        await memory.record_success(TENANT, "a.example", 3)  # must not raise


def test_default_hint_lifetime_is_a_day():
    """Round 64 — staleness is the re-probe's job; the TTL only decides
    whether a crawl started later the same day starts from scratch."""
    config = EscalationConfig()
    assert config.level_memory_ttl_seconds == 86400
    assert config.reprobe_every == 20
