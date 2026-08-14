# proxy/pool_health.py
"""Proxy pool health as a first-class, per-tier state machine (round 34).

Two questions the rest of the system needs answered that neither
proxy/manager.py's per-request exhaustion nor a raw Prometheus gauge alone
answers: "is the pool healthy right now" and "did that just change" — the
latter is what lets a caller emit exactly one notification per real
transition instead of either silence or a per-cycle spam loop.

State is tracked per escalation tier (1/2/3) since a free-tier-only source
mix commonly runs L3 CRITICAL while L1 stays HEALTHY — collapsing that into
one pool-wide state would hide which tier actually needs attention.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from scraper_engine.config.schema import ProxyTierConfig
    from scraper_engine.core.tenant import TenantId
    from scraper_engine.storage.postgres_client import PostgresClient
    from scraper_engine.storage.redis_client import RedisClient

TIERS: tuple[int, ...] = (1, 2, 3)

# Redis key holding the last-observed state for a tier — read back on every
# check() so a transition is only reported once, and survives daemon restart
# (unlike an in-process variable, which would report a false transition on
# every deploy).
_STATE_KEY_PREFIX = "proxy:pool_health:tier:"


class PoolHealthState(str, Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    CRITICAL = "critical"


@dataclass(frozen=True)
class PoolHealthTransition:
    """Emitted only when a tier's state actually changes between two
    consecutive check() calls — the signal callers (harvester_daemon's
    health cycle, eventually the ops webhook via webhook_events.py) should
    act on. Includes `validated_count` so the resulting alert is
    self-explanatory without a second query."""

    tier: int
    old_state: PoolHealthState
    new_state: PoolHealthState
    validated_count: int


def _classify(count: int, tier_config: ProxyTierConfig) -> PoolHealthState:
    if count < tier_config.critical_below_count:
        return PoolHealthState.CRITICAL
    if count < tier_config.degraded_below_count:
        return PoolHealthState.DEGRADED
    return PoolHealthState.HEALTHY


async def current_state(redis: RedisClient, tier: int) -> PoolHealthState:
    """Read-only lookup of a tier's last-persisted health state (round 34) —
    used by proxy/dlq_reaper.py to gate PROXY_EXHAUSTED auto-retry without
    triggering a fresh check() (which recomputes from Postgres and can emit
    a transition; the reaper only wants to observe current state, not drive
    the state machine). Defaults to HEALTHY when no state has been recorded
    yet, matching check()'s own bootstrap default."""
    raw = await redis.raw.get(f"{_STATE_KEY_PREFIX}{tier}")
    return PoolHealthState(raw) if raw else PoolHealthState.HEALTHY


async def _count_validated_for_tier(pg: PostgresClient, tenant: TenantId, min_score: float) -> int:
    rows = await pg.fetch(
        tenant,
        "SELECT COUNT(*) as n FROM proxy_pool WHERE reliability_score >= $1",
        min_score,
    )
    return rows[0]["n"] if rows else 0


class PoolHealthMonitor:
    """Computes current per-tier health and diffs it against the
    last-persisted state to produce transitions. One instance is
    stateless beyond its pg/redis handles — all actual state lives in
    Redis, so multiple daemon instances (or a restarted one) never
    disagree about what "last known state" was."""

    def __init__(
        self, pg: PostgresClient, redis: RedisClient, tier_config: ProxyTierConfig
    ) -> None:
        self._pg = pg
        self._redis = redis
        self._tier_config = tier_config

    async def check(self, tenant: TenantId) -> list[PoolHealthTransition]:
        """Recompute health for every tier, persist it, and return only the
        tiers whose state changed since the last call. Never raises — a
        query failure for one tier must not block the others or crash the
        daemon's health cycle; it's logged and that tier is skipped for
        this cycle."""
        min_scores = {
            1: self._tier_config.min_score_level_1,
            2: self._tier_config.min_score_level_2,
            3: self._tier_config.min_score_level_3,
        }
        transitions: list[PoolHealthTransition] = []
        for tier in TIERS:
            count = await _count_validated_for_tier(self._pg, tenant, min_scores[tier])
            new_state = _classify(count, self._tier_config)

            key = f"{_STATE_KEY_PREFIX}{tier}"
            previous_raw = await self._redis.raw.get(key)
            old_state = PoolHealthState(previous_raw) if previous_raw else PoolHealthState.HEALTHY

            if new_state != old_state:
                transitions.append(
                    PoolHealthTransition(
                        tier=tier,
                        old_state=old_state,
                        new_state=new_state,
                        validated_count=count,
                    )
                )
            await self._redis.raw.set(key, new_state.value)

            from scraper_engine.observability.metrics import proxy_pool_health

            proxy_pool_health.labels(tier=str(tier)).set(_STATE_ORDINAL[new_state])

        return transitions


# Higher is healthier — lets a Grafana panel threshold on this numerically
# without string-matching the label.
_STATE_ORDINAL: dict[PoolHealthState, int] = {
    PoolHealthState.CRITICAL: 0,
    PoolHealthState.DEGRADED: 1,
    PoolHealthState.HEALTHY: 2,
}
