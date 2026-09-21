# orchestrator/level_memory.py
"""Cross-job memory of which escalation level actually works for a domain.

Round 63. `orchestrator/worker.py` entered the L1->L2->L3 ladder at L1 for
every URL of every job. Nothing anywhere remembered that the previous fifty
URLs of the same domain had all needed L3 — `level_used` was written to
`scrape_results` but only ever read back by the URL-exact cache check, never
to decide where to start. For a target that only succeeds in a real browser
that means one doomed HTTP attempt plus one doomed Botasaurus launch before
every single fetch that can work.

Measured by an external consumer against Jumia Nigeria: 169.2s in PROCESSING
for a page whose real fetch took 27.6s, with all 49 successful fetches
landing at L3. ~84% of the job was the ladder, not the work.

Two invariants keep this safe to have on by default:

  * It only ever SKIPS levels that recently failed for this domain. The
    ladder above the hint is untouched, so a hint can make a job faster and
    can never turn a fetch that would have succeeded into a failure.
  * It re-probes. `reprobe_every` URLs, one runs the full ladder regardless
    of the hint, so a target whose defences relax is rediscovered instead of
    paying for a browser forever. The TTL alone would not do this: a
    continuously crawled domain refreshes its hint before it can ever expire.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from scraper_engine.config.schema import EscalationConfig
    from scraper_engine.core.tenant import TenantId

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DomainPlan:
    """What level memory says about the next URL of a domain (round 64)."""

    start_level: int
    # The free proxy pool is refused here; go straight to the paid gateway.
    skip_pool: bool = False
    # Botasaurus keeps failing here at L2; use L2's Camoufox engine directly.
    skip_botasaurus: bool = False


class LevelMemory:
    """Per-(tenant, domain) "start the ladder here" hint, stored in Redis.

    Every method is a no-op returning the neutral value when
    `escalation.level_memory_enabled` is false, so callers never branch on
    the toggle themselves.

    Redis failures are swallowed and logged: a missing hint costs latency,
    never correctness, and this must not be able to fail a job that would
    otherwise have run fine.
    """

    def __init__(self, redis: Any, config: EscalationConfig) -> None:
        # Takes the RedisClient, not its .raw — the hint keys carry the tenant
        # in their own name (same shape as orchestrator/politeness.py's), so
        # they must NOT go through RedisClient's tenant-prefixing wrapper, but
        # resolving .raw lazily per call keeps constructing a Worker from
        # failing when Redis has not been started yet.
        self._redis = redis
        self._config = config

    @property
    def _raw(self) -> Any:
        return self._redis.raw

    @staticmethod
    def _hint_key(tenant_id: TenantId, domain: str) -> str:
        return f"levelhint:{tenant_id}:{domain}"

    @staticmethod
    def _probe_key(tenant_id: TenantId, domain: str) -> str:
        return f"levelhint:probe:{tenant_id}:{domain}"

    @staticmethod
    def _pool_block_key(tenant_id: TenantId, domain: str) -> str:
        return f"levelhint:poolblock:{tenant_id}:{domain}"

    @staticmethod
    def _botasaurus_fails_key(tenant_id: TenantId, domain: str) -> str:
        return f"levelhint:botafail:{tenant_id}:{domain}"

    async def plan(self, tenant_id: TenantId, domain: str, levels: list[int]) -> DomainPlan:
        """What to skip for the next URL of `domain`.

        Returns the neutral plan — `levels[0]`, nothing skipped — when memory
        is disabled, when this call is the periodic re-probe, or on any Redis
        error. One re-probe counter covers every hint, so a re-probe URL
        retries the full ladder, the free pool AND Botasaurus. A level hint
        outside `levels` (the caller narrowed the ladder with max_level) is
        clamped into it rather than ignored, so `max_level=2` against an L3
        hint still starts at 2.

        Round 64 added the two skip hints, each measured live on Jumia before
        it existed: every URL paid a doomed free-pool L2 attempt (two
        browsers, 40-94s, always 403) before the gateway retry that always
        worked, and every L2 attempt ran Botasaurus first, which failed 13 of
        13 times (CloudflareDetectionException, 26-85s each) before L2's
        Camoufox fetched the page.
        """
        neutral = DomainPlan(start_level=levels[0])
        if not self._config.level_memory_enabled:
            return neutral
        try:
            if await self._is_reprobe(tenant_id, domain):
                return neutral
            raw = await self._raw.get(self._hint_key(tenant_id, domain))
            pool_blocked = await self._raw.get(self._pool_block_key(tenant_id, domain))
            bota_fails = await self._raw.get(self._botasaurus_fails_key(tenant_id, domain))
        except Exception:
            logger.warning("level_memory_read_failed domain=%s", domain, exc_info=True)
            return neutral
        return DomainPlan(
            start_level=self._clamp(raw, levels),
            skip_pool=pool_blocked is not None,
            skip_botasaurus=bota_fails is not None,
        )

    async def start_level(self, tenant_id: TenantId, domain: str, levels: list[int]) -> int:
        """The level part of plan()."""
        return (await self.plan(tenant_id, domain, levels)).start_level

    @staticmethod
    def _clamp(raw: Any, levels: list[int]) -> int:
        if raw is None:
            return levels[0]
        try:
            hint = int(raw)
        except (TypeError, ValueError):
            return levels[0]
        # Never start below the caller's own floor, never above its ceiling.
        return min(max(hint, levels[0]), levels[-1])

    async def _is_reprobe(self, tenant_id: TenantId, domain: str) -> bool:
        """True once every `reprobe_every` calls for this domain."""
        every = self._config.reprobe_every
        if every <= 1:
            # 0 disables re-probing; 1 would mean "always re-probe", which is
            # indistinguishable from disabling the memory entirely.
            return False
        count = int(await self._raw.incr(self._probe_key(tenant_id, domain)))
        if count == 1:
            # First URL for this domain in this window — give the counter the
            # same lifetime as the hint it guards, so both age out together.
            await self._raw.expire(
                self._probe_key(tenant_id, domain), self._config.level_memory_ttl_seconds
            )
        return count % every == 0

    async def record_success(self, tenant_id: TenantId, domain: str, level: int) -> None:
        """Remember that `level` is what worked for `domain`.

        Level 1 deletes the hint rather than storing it: L1 is where the
        ladder already starts, so "start at 1" carries no information, and
        writing it would keep a stale higher hint alive. Deleting is how a
        re-probe that succeeds lower actually takes effect.
        """
        if not self._config.level_memory_enabled:
            return
        key = self._hint_key(tenant_id, domain)
        try:
            if level <= 1:
                await self._raw.delete(key)
            else:
                await self._raw.set(
                    key, str(level), ex=self._config.level_memory_ttl_seconds
                )
        except Exception:
            logger.warning("level_memory_write_failed domain=%s", domain, exc_info=True)

    async def record_pool_blocked(self, tenant_id: TenantId, domain: str) -> None:
        """A free-pool attempt was blocked and the gateway retry at the same
        level was not — this domain refuses the pool, not the level."""
        await self._set_flag(self._pool_block_key(tenant_id, domain), domain)

    async def record_pool_ok(self, tenant_id: TenantId, domain: str) -> None:
        """A free-pool proxy produced usable content — drop the pool hint."""
        await self._clear_flag(self._pool_block_key(tenant_id, domain), domain)

    async def record_botasaurus_failed(self, tenant_id: TenantId, domain: str) -> None:
        """L2 fell back from Botasaurus and its Camoufox engine succeeded."""
        await self._set_flag(self._botasaurus_fails_key(tenant_id, domain), domain)

    async def record_botasaurus_ok(self, tenant_id: TenantId, domain: str) -> None:
        """Botasaurus produced the accepted L2 result — drop the hint."""
        await self._clear_flag(self._botasaurus_fails_key(tenant_id, domain), domain)

    async def _set_flag(self, key: str, domain: str) -> None:
        if not self._config.level_memory_enabled:
            return
        try:
            await self._raw.set(key, "1", ex=self._config.level_memory_ttl_seconds)
        except Exception:
            logger.warning("level_memory_write_failed domain=%s", domain, exc_info=True)

    async def _clear_flag(self, key: str, domain: str) -> None:
        if not self._config.level_memory_enabled:
            return
        try:
            await self._raw.delete(key)
        except Exception:
            logger.warning("level_memory_write_failed domain=%s", domain, exc_info=True)
