# orchestrator/host_capacity.py
"""Host-wide browser admission (round 65).

One browser budget per HOST, shared by every worker process on it. Before
this, each rq work-horse sized core.budget.BROWSER_SEMAPHORE as if it owned
the machine: 3 worker containers x 5 concurrent URLs put 15 renders on a
4-core host (load avg 58-69, measured live) and slowed every one of them.
The local semaphore stays as a per-process safety net; this is the limit
that actually binds.

A worker claims before every browser render. One Lua script (CLAIM) grants
three things together, or none of them:

  - a seat: `weight` units of the host's budget (target from the capacity
    controller, orchestrator/capacity_controller.py, or a static default
    while it is down);
  - the website's politeness slot, on the same key
    orchestrator/politeness.py uses, against THIS request's own cap;
  - the website's inter-fetch delay (now >= next allowed instant).

Nothing is ever held while waiting for something else — that is what made a
URL sit on its politeness slot during an untimed browser wait and starve its
same-site siblings into the 300s slot timeout.

Rules the script enforces:
  - self-claim only: it never grants to anyone but the caller, so a waiter
    that died can never be handed a seat;
  - no jumping the line: the caller wins only if no OLDER waiter could claim
    right now (older = earlier first-enqueue time, kept across a URL's
    levels and retries so escalating never loses its place);
  - a waiter whose website is full or still inside its delay is skipped, so
    one busy site never blocks the line;
  - while another tenant is waiting, one tenant holds at most
    ceil(target * tenant_share) units;
  - the first render on an idle host is always admitted, whatever its
    weight, so a weight above the target can never deadlock.

Every seat and slot is a lease with its own expiry (Redis TIME), renewed by
the holder and purged by every script once it lapses, so a killed process's
seats come back within one lease TTL. Waiters expire the same way.

Scripts build some key names from waiter data (other waiters' slot and delay
keys). That is fine on this deployment's single Redis and is NOT Redis
Cluster / ACL-key safe.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import json
import logging
import os
import random
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from scraper_engine.orchestrator.politeness import delay_key, slot_key

if TYPE_CHECKING:
    from scraper_engine.config.schema import HostCapacityConfig
    from scraper_engine.core.tenant import TenantId

logger = logging.getLogger(__name__)

# Housekeeping TTL for the host's own keys. Refreshed on every claim and
# renewal, so it only lapses once a host has gone quiet (or been rebooted,
# which changes its host id).
_HOST_KEY_TTL_MS = 3_600_000

# Upper edges (ms) of the admission-wait histogram buckets kept in Redis, for
# observability/metrics.py to export at scrape time — worker processes exit
# after every job, so an in-process histogram would never be scraped.
WAIT_BUCKETS_MS = (1_000, 5_000, 30_000, 120_000, 600_000)

_LUA_PRELUDE = """
local t = redis.call('TIME')
local now = tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)
local function extend_ttl(k, ms)
    if redis.call('PTTL', k) < ms then
        redis.call('PEXPIRE', k, ms)
    end
end
"""

# KEYS: seats, seat_weight, seat_tenant, waiters, waiter_exp, waiter_info,
#       target, stats
# ARGV: waiter_id, priority_ms, info_json, lease_id, lease_ttl_ms,
#       waiter_ttl_ms, default_target, tenant_share, host_key_ttl_ms,
#       wait_ms, wait_bucket
# Returns {granted(0|1), in_use, target, waiters} as strings (a Lua number
# reply would be truncated to an integer by Redis).
CLAIM_LUA = (
    _LUA_PRELUDE
    + """
local seats, sw, st = KEYS[1], KEYS[2], KEYS[3]
local waiters, wexp, winfo = KEYS[4], KEYS[5], KEYS[6]
local target_key, stats = KEYS[7], KEYS[8]
local me, prio, info = ARGV[1], tonumber(ARGV[2]), ARGV[3]
local lease, lease_ttl = ARGV[4], tonumber(ARGV[5])
local waiter_ttl, default_target = tonumber(ARGV[6]), tonumber(ARGV[7])
local share, keep = tonumber(ARGV[8]), tonumber(ARGV[9])

-- Drop lapsed seats and waiters first: a crashed holder frees its seat here.
local dead = redis.call('ZRANGEBYSCORE', seats, '-inf', now)
for _, l in ipairs(dead) do
    redis.call('ZREM', seats, l)
    redis.call('HDEL', sw, l)
    redis.call('HDEL', st, l)
end
local gone = redis.call('ZRANGEBYSCORE', wexp, '-inf', now)
for _, w in ipairs(gone) do
    redis.call('ZREM', wexp, w)
    redis.call('ZREM', waiters, w)
    redis.call('HDEL', winfo, w)
end

-- Join (or stay in) the line. NX keeps the first priority this waiter got.
redis.call('ZADD', waiters, 'NX', prio, me)
redis.call('ZADD', wexp, now + waiter_ttl, me)
redis.call('HSET', winfo, me, info)

local in_use = 0
local tenant_use = {}
local held = redis.call('HGETALL', sw)
for i = 1, #held, 2 do
    local w = tonumber(held[i + 1])
    local tn = redis.call('HGET', st, held[i]) or ''
    in_use = in_use + w
    tenant_use[tn] = (tenant_use[tn] or 0) + w
end

local target = default_target
local raw_target = redis.call('GET', target_key)
if raw_target then
    target = tonumber(raw_target) or default_target
end

local line = redis.call('ZRANGE', waiters, 0, -1)
local infos = {}
local tenants = {}
local n_tenants = 0
for _, w in ipairs(line) do
    local raw = redis.call('HGET', winfo, w)
    if raw then
        local inf = cjson.decode(raw)
        infos[w] = inf
        if not tenants[inf.tenant] then
            tenants[inf.tenant] = true
            n_tenants = n_tenants + 1
        end
    end
end
local share_cap = math.max(1, math.ceil(target * share))

local slot_count = {}
local next_at = {}
local function slots(k)
    if slot_count[k] == nil then
        slot_count[k] = redis.call('ZCOUNT', k, '(' .. now, '+inf')
    end
    return slot_count[k]
end
local function nxt(k)
    if next_at[k] == nil then
        next_at[k] = tonumber(redis.call('GET', k) or '0') or 0
    end
    return next_at[k]
end

local sim_use = in_use
for _, w in ipairs(line) do
    local inf = infos[w]
    if inf then
        local ok = slots(inf.slot_key) < inf.cap and now >= nxt(inf.delay_key)
        if ok and sim_use > 0 and sim_use + inf.weight > target then
            ok = false
        end
        if ok and n_tenants > 1 then
            local tu = tenant_use[inf.tenant] or 0
            if tu > 0 and tu + inf.weight > share_cap then
                ok = false
            end
        end
        if w == me then
            if not ok then
                return {'0', tostring(in_use), tostring(target), tostring(#line)}
            end
            redis.call('ZADD', seats, now + lease_ttl, lease)
            redis.call('HSET', sw, lease, tostring(inf.weight))
            redis.call('HSET', st, lease, inf.tenant)
            redis.call('ZADD', inf.slot_key, now + lease_ttl, lease)
            extend_ttl(inf.slot_key, lease_ttl)
            if inf.delay_ms > 0 then
                redis.call('SET', inf.delay_key, tostring(now + inf.delay_ms),
                           'PX', math.max(60000, inf.delay_ms * 4))
            end
            redis.call('ZREM', waiters, me)
            redis.call('ZREM', wexp, me)
            redis.call('HDEL', winfo, me)
            redis.call('HINCRBY', stats, 'granted', 1)
            redis.call('HINCRBY', stats, 'wait_ms_sum', ARGV[10])
            redis.call('HINCRBY', stats, 'wait_le_' .. ARGV[11], 1)
            for _, k in ipairs({seats, sw, st, waiters, wexp, winfo, stats}) do
                extend_ttl(k, keep)
            end
            return {'1', tostring(in_use + inf.weight), tostring(target), tostring(#line - 1)}
        end
        if ok then
            -- An older eligible waiter will take this on its next poll.
            sim_use = sim_use + inf.weight
            tenant_use[inf.tenant] = (tenant_use[inf.tenant] or 0) + inf.weight
            slot_count[inf.slot_key] = slots(inf.slot_key) + 1
            if inf.delay_ms > 0 then
                next_at[inf.delay_key] = now + inf.delay_ms
            end
        end
    end
end
for _, k in ipairs({waiters, wexp, winfo}) do
    extend_ttl(k, keep)
end
return {'0', tostring(in_use), tostring(target), tostring(#line)}
"""
)

# KEYS: seats, slot_key   ARGV: lease_id, lease_ttl_ms, host_key_ttl_ms
RENEW_LUA = (
    _LUA_PRELUDE
    + """
local lease, ttl, keep = ARGV[1], tonumber(ARGV[2]), tonumber(ARGV[3])
local expires_at = tonumber(redis.call('ZSCORE', KEYS[1], lease) or '0')
if expires_at <= now then
    return 0
end
redis.call('ZADD', KEYS[1], 'XX', now + ttl, lease)
extend_ttl(KEYS[1], keep)
if redis.call('ZSCORE', KEYS[2], lease) then
    redis.call('ZADD', KEYS[2], 'XX', now + ttl, lease)
    extend_ttl(KEYS[2], ttl)
end
return 1
"""
)

# KEYS: seats, seat_weight, seat_tenant, slot_key   ARGV: lease_id
RELEASE_LUA = """
redis.call('ZREM', KEYS[1], ARGV[1])
redis.call('HDEL', KEYS[2], ARGV[1])
redis.call('HDEL', KEYS[3], ARGV[1])
redis.call('ZREM', KEYS[4], ARGV[1])
return 1
"""

# KEYS: waiters, waiter_exp, waiter_info   ARGV: waiter_id
LEAVE_LUA = """
redis.call('ZREM', KEYS[1], ARGV[1])
redis.call('ZREM', KEYS[2], ARGV[1])
redis.call('HDEL', KEYS[3], ARGV[1])
return 1
"""

# KEYS: seats, seat_weight, waiter_exp, target   ARGV: default_target
# Read-only: live units in use, live waiters, current target.
STATUS_LUA = (
    _LUA_PRELUDE
    + """
local in_use = 0
for _, l in ipairs(redis.call('ZRANGEBYSCORE', KEYS[1], '(' .. now, '+inf')) do
    in_use = in_use + (tonumber(redis.call('HGET', KEYS[2], l) or '0') or 0)
end
local waiting = redis.call('ZCOUNT', KEYS[3], '(' .. now, '+inf')
local target = tonumber(redis.call('GET', KEYS[4]) or '') or tonumber(ARGV[1])
return {tostring(in_use), tostring(waiting), tostring(target)}
"""
)

# Set while this task holds a claim. A claim taken inside another one can
# deadlock a full host (the inner one waits for a seat only the outer one
# could free), so it is refused outright instead.
_CLAIM_HELD: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "host_capacity_claim_held", default=False
)


class AdmissionError(Exception):
    """Base for every way a claim can end without a grant."""


class AdmissionUnavailableError(AdmissionError):
    """Redis (or a script) failed. Transient, and not the target's fault —
    callers must not count it against the domain's circuit breaker."""


class AdmissionTimeoutError(AdmissionError):
    """The wait budget ran out before a seat and slot came free together."""

    def __init__(self, waited_ms: int) -> None:
        super().__init__(f"No browser capacity within {waited_ms / 1000:.0f}s")
        self.waited_ms = waited_ms


class AdmissionCancelledError(AdmissionError):
    """The job was cancelled while this claim was waiting."""


class NestedClaimError(RuntimeError):
    """A claim was attempted while this task already holds one."""


@dataclass(frozen=True)
class Grant:
    lease_id: str
    wait_ms: int
    in_use: float
    target: float


@dataclass(frozen=True)
class CapacitySnapshot:
    in_use: float
    waiters: int
    target: float


def _wait_bucket(wait_ms: int) -> str:
    for edge in WAIT_BUCKETS_MS:
        if wait_ms <= edge:
            return str(edge)
    return "inf"


class HostAdmission:
    """Claims browser capacity on this host. One instance per process."""

    def __init__(
        self,
        redis: Any,
        host_id: str,
        config: HostCapacityConfig,
        *,
        cpu_count: int | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._redis = redis
        self._cfg = config
        self._clock = clock
        self._sleep = sleep
        cpus = cpu_count or os.cpu_count() or 1
        self.default_target = config.default_units or float(cpus)
        prefix = f"hc:{host_id}"
        self.seats_key = f"{prefix}:seats"
        self.seat_weight_key = f"{prefix}:seat_weight"
        self.seat_tenant_key = f"{prefix}:seat_tenant"
        self.waiters_key = f"{prefix}:waiters"
        self.waiter_exp_key = f"{prefix}:waiter_exp"
        self.waiter_info_key = f"{prefix}:waiter_info"
        self.target_key = f"{prefix}:target"
        self.stats_key = f"{prefix}:stats"

    async def _eval(self, script: str, keys: list[str], args: list[Any]) -> Any:
        try:
            return await self._redis.eval(script, len(keys), *keys, *args)
        except Exception as exc:  # CancelledError is BaseException: never caught here
            raise AdmissionUnavailableError(f"host admission unavailable: {exc}") from exc

    @contextlib.asynccontextmanager
    async def claim(
        self,
        *,
        tenant_id: TenantId,
        domain: str,
        weight: float,
        concurrency: int,
        delay_seconds: float,
        priority_ms: int,
        wait_budget_seconds: float,
        is_cancelled: Callable[[], Awaitable[bool]] | None = None,
    ) -> AsyncIterator[Grant]:
        """Wait for a seat + slot + delay on `domain`, hold them for the block.

        Raises AdmissionTimeoutError / AdmissionCancelledError / AdmissionUnavailableError
        without ever having held anything. On exit (including cancellation)
        the lease is released; if that release itself fails, the lease
        lapses on its own within lease_ttl_seconds.
        """
        if _CLAIM_HELD.get():
            raise NestedClaimError("a host capacity claim is already held by this task")
        turn_key = slot_key(domain, tenant_id)
        grant = await self._wait_for_grant(
            tenant_id=tenant_id,
            turn_key=turn_key,
            delay_key_name=delay_key(domain, tenant_id),
            weight=weight,
            concurrency=concurrency,
            delay_seconds=delay_seconds,
            priority_ms=priority_ms,
            wait_budget_seconds=wait_budget_seconds,
            is_cancelled=is_cancelled,
        )
        token = _CLAIM_HELD.set(True)
        renewer = asyncio.create_task(self._renew_loop(grant.lease_id, turn_key))
        try:
            yield grant
        finally:
            _CLAIM_HELD.reset(token)
            renewer.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await renewer
            try:
                await self._eval(
                    RELEASE_LUA,
                    [self.seats_key, self.seat_weight_key, self.seat_tenant_key, turn_key],
                    [grant.lease_id],
                )
            except AdmissionUnavailableError:
                logger.warning(
                    "host_claim_release_failed lease=%s — it lapses within %ds",
                    grant.lease_id,
                    self._cfg.lease_ttl_seconds,
                )

    async def _wait_for_grant(
        self,
        *,
        tenant_id: TenantId,
        turn_key: str,
        delay_key_name: str,
        weight: float,
        concurrency: int,
        delay_seconds: float,
        priority_ms: int,
        wait_budget_seconds: float,
        is_cancelled: Callable[[], Awaitable[bool]] | None,
    ) -> Grant:
        cfg = self._cfg
        waiter_id = uuid.uuid4().hex
        lease_id = uuid.uuid4().hex
        info = json.dumps(
            {
                "tenant": str(tenant_id),
                "slot_key": turn_key,
                "delay_key": delay_key_name,
                "cap": max(1, concurrency),
                "delay_ms": max(0, int(delay_seconds * 1000)),
                "weight": weight,
            }
        )
        started = self._clock()
        deadline = started + max(0.0, wait_budget_seconds)
        next_cancel_check = started + cfg.cancel_check_interval_seconds
        keys = [
            self.seats_key,
            self.seat_weight_key,
            self.seat_tenant_key,
            self.waiters_key,
            self.waiter_exp_key,
            self.waiter_info_key,
            self.target_key,
            self.stats_key,
        ]
        try:
            while True:
                waited_ms = int((self._clock() - started) * 1000)
                reply = await self._eval(
                    CLAIM_LUA,
                    keys,
                    [
                        waiter_id,
                        priority_ms,
                        info,
                        lease_id,
                        cfg.lease_ttl_seconds * 1000,
                        cfg.waiter_ttl_seconds * 1000,
                        self.default_target,
                        cfg.tenant_share,
                        _HOST_KEY_TTL_MS,
                        waited_ms,
                        _wait_bucket(waited_ms),
                    ],
                )
                if str(reply[0]) == "1":
                    return Grant(
                        lease_id=lease_id,
                        wait_ms=waited_ms,
                        in_use=float(reply[1]),
                        target=float(reply[2]),
                    )
                now = self._clock()
                if now >= deadline:
                    await self._count_timeout()
                    raise AdmissionTimeoutError(int((now - started) * 1000))
                if is_cancelled is not None and now >= next_cancel_check:
                    next_cancel_check = now + cfg.cancel_check_interval_seconds
                    if await is_cancelled():
                        raise AdmissionCancelledError("job cancelled while waiting for capacity")
                pause = random.uniform(cfg.poll_min_seconds, cfg.poll_max_seconds)
                await self._sleep(min(pause, max(0.0, deadline - now)))
        except BaseException:
            # Leave the line on every non-grant exit, including cancellation.
            # Best effort: a waiter left behind lapses within waiter_ttl.
            with contextlib.suppress(Exception):
                await self._redis.eval(
                    LEAVE_LUA,
                    3,
                    self.waiters_key,
                    self.waiter_exp_key,
                    self.waiter_info_key,
                    waiter_id,
                )
            raise

    async def _count_timeout(self) -> None:
        with contextlib.suppress(Exception):
            await self._redis.hincrby(self.stats_key, "timeouts", 1)

    async def _renew_loop(self, lease_id: str, turn_key: str) -> None:
        """Keep the lease alive while held. Stops — never cancels the render —
        once max_hold_seconds is reached, or when the lease is found gone."""
        cfg = self._cfg
        started = self._clock()
        while True:
            await self._sleep(cfg.renew_interval_seconds)
            if self._clock() - started >= cfg.max_hold_seconds:
                logger.warning(
                    "seat_overheld lease=%s held>%ds — no longer renewed, lapses within %ds",
                    lease_id,
                    cfg.max_hold_seconds,
                    cfg.lease_ttl_seconds,
                )
                return
            try:
                alive = await self._eval(
                    RENEW_LUA,
                    [self.seats_key, turn_key],
                    [lease_id, cfg.lease_ttl_seconds * 1000, _HOST_KEY_TTL_MS],
                )
            except AdmissionUnavailableError:
                logger.warning("host_claim_renew_failed lease=%s", lease_id)
                continue
            if not int(alive):
                logger.warning("host_claim_lease_lost lease=%s", lease_id)
                return

    async def snapshot(self) -> CapacitySnapshot:
        """Live units in use, live waiters, and the current target."""
        reply = await self._eval(
            STATUS_LUA,
            [self.seats_key, self.seat_weight_key, self.waiter_exp_key, self.target_key],
            [self.default_target],
        )
        return CapacitySnapshot(
            in_use=float(reply[0]), waiters=int(reply[1]), target=float(reply[2])
        )
