# orchestrator/capacity_controller.py
"""Sizes the host-wide browser budget from real host pressure (round 65).

orchestrator/host_capacity.py hands out browser seats up to a target number
of units per host. This daemon sets that target, so nobody hand-picks it:
every `controller_interval_seconds` it reads the host's CPU pressure (Linux
PSI, `/proc/pressure/cpu` "some avg10" — containers see the HOST's value, not
their own, verified live) and available memory, then

  - when someone is waiting for a seat and the last change is at least
    `raise_dwell_seconds` old, raises the target — under `cpu_pressure_low`
    straight to everyone waiting, between the two marks by
    `raise_step_fraction` of itself; never past what free memory can hold
    (round 66: it used to add one unit per 30s and only under the low mark,
    so after any cut it froze in between — live, 2-4 browsers on an idle
    host);
  - cuts it by `cut_factor` when CPU pressure is over `cpu_pressure_high` or
    MemAvailable is under `mem_available_floor_mb`, at most once per
    `cut_dwell_seconds`;
  - otherwise holds it.

Pressure is used as a brake above/below two marks, not as a gauge: CPU PSI
saturates once runnable work exceeds the cores, so it cannot tell 2x from
10x oversubscription, only "too much" from "room to spare". Where PSI is
unavailable (kernel booted with psi=0, some sandboxes) the 1-minute load
average per core stands in, mapped onto the same 0-100 scale.

Because it measures the whole host, other programs' load shrinks our share
too — we yield to them; nothing here can control them.

State lives in Redis, not in this process: the target (with a TTL, so a dead
controller decays to host_capacity's static default instead of freezing a
stale value), the last-change time (so a new leader keeps the dwell), and a
leader lock keyed by host id, so a second copy of this daemon on the same
host only stands by.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from scraper_engine.config.loader import load_config
from scraper_engine.core.host_identity import resolve_host_id
from scraper_engine.core.periodic import run_periodic
from scraper_engine.observability.bootstrap import bootstrap_observability
from scraper_engine.orchestrator.host_capacity import HostAdmission
from scraper_engine.storage.redis_client import RedisClient

if TYPE_CHECKING:
    from scraper_engine.config.schema import AppConfig, HostCapacityConfig

logger = logging.getLogger(__name__)

_CPU_PRESSURE_PATH = Path("/proc/pressure/cpu")
_MEMINFO_PATH = Path("/proc/meminfo")

# Take the lock if free, or extend it if it is already ours. Returns 1 when
# this caller leads.
LEAD_LUA = """
local held = redis.call('GET', KEYS[1])
if held == ARGV[1] then
    redis.call('PEXPIRE', KEYS[1], ARGV[2])
    return 1
end
if not held then
    redis.call('SET', KEYS[1], ARGV[1], 'PX', ARGV[2])
    return 1
end
return 0
"""


def read_cpu_pressure(path: Path = _CPU_PRESSURE_PATH) -> float | None:
    """PSI "some avg10" (percent of the last 10s some task waited for CPU)."""
    try:
        text = path.read_text()
    except OSError:
        return None
    for line in text.splitlines():
        if line.startswith("some "):
            for field in line.split()[1:]:
                key, _, value = field.partition("=")
                if key == "avg10":
                    return float(value)
    return None


def load_pressure(
    cpu_count: int, getloadavg: Callable[[], tuple[float, float, float]] = os.getloadavg
) -> float | None:
    """Fallback when PSI is unavailable: 1-minute load per core above 1.0,
    scaled so 1.4 runnable tasks per core reads as 40 and 1.8 as 80."""
    try:
        load = getloadavg()[0]
    except OSError:
        return None
    return max(0.0, load / max(1, cpu_count) - 1.0) * 100


def read_mem_available_mb(path: Path = _MEMINFO_PATH) -> int | None:
    try:
        text = path.read_text()
    except OSError:
        return None
    for line in text.splitlines():
        if line.startswith("MemAvailable:"):
            return int(line.split()[1]) // 1024
    return None


def read_mem_total_mb(path: Path = _MEMINFO_PATH) -> int | None:
    try:
        text = path.read_text()
    except OSError:
        return None
    for line in text.splitlines():
        if line.startswith("MemTotal:"):
            return int(line.split()[1]) // 1024
    return None


def memory_ceiling(cfg: HostCapacityConfig, cpu_count: int, mem_total_mb: int | None) -> float:
    """The most units this host may ever run. An explicit `max_units` wins;
    otherwise as many browsers as total memory holds (round 66 — it was
    2 x CPUs, which on light pages was the limit that bound, not pressure:
    live, 8 seats on a host that ran 18 browsers at CPU PSI 27 unlimited).
    Without a memory reading, the old 2 x CPUs."""
    if cfg.max_units is not None:
        return cfg.max_units
    if mem_total_mb is None:
        return float(2 * cpu_count)
    return float(max(cfg.min_units, mem_total_mb // cfg.browser_memory_mb))


def decide(
    current: float,
    *,
    cpu_pressure: float | None,
    mem_available_mb: int | None,
    waiters: int,
    in_use: float,
    since_change: float,
    cfg: HostCapacityConfig,
    max_units: float,
) -> float:
    """The next target. Pure: every input is passed in."""
    current = min(max(current, cfg.min_units), max_units)
    strained = (cpu_pressure is not None and cpu_pressure > cfg.cpu_pressure_high) or (
        mem_available_mb is not None and mem_available_mb < cfg.mem_available_floor_mb
    )
    if strained:
        if since_change >= cfg.cut_dwell_seconds:
            return max(cfg.min_units, round(current * cfg.cut_factor, 2))
        return current
    if cpu_pressure is None or waiters == 0 or since_change < cfg.raise_dwell_seconds:
        return current
    # Round 66 — work is waiting and nothing is strained: let it in. Calm, the
    # whole queue at once (one unit per dwell made short jobs finish before
    # the target caught up: +60-100% wall time on light pages). Between the
    # marks, a step at a time — the old rule held there, and after a cut the
    # target never came back up.
    wanted = in_use + waiters
    if cpu_pressure >= cfg.cpu_pressure_low:
        wanted = min(wanted, current + max(1.0, current * cfg.raise_step_fraction))
    if mem_available_mb is not None:
        spare = max(0, mem_available_mb - cfg.mem_available_floor_mb)
        # Whole browsers only: a browser needs all of its memory.
        wanted = min(wanted, in_use + spare // cfg.browser_memory_mb)
    return max(current, min(max_units, round(wanted, 2)))


class CapacityController:
    def __init__(
        self,
        redis: RedisClient,
        host_id: str,
        cfg: HostCapacityConfig,
        *,
        cpu_count: int | None = None,
        clock: Callable[[], float] = time.time,
        cpu_pressure_reader: Callable[[], float | None] = read_cpu_pressure,
        mem_reader: Callable[[], int | None] = read_mem_available_mb,
        load_reader: Callable[[int], float | None] = load_pressure,
    ) -> None:
        self._redis = redis
        self._cfg = cfg
        self._cpus = cpu_count or os.cpu_count() or 1
        self._clock = clock
        self._read_cpu = cpu_pressure_reader
        self._read_mem = mem_reader
        self._read_load = load_reader
        self._admission = HostAdmission(redis.raw, host_id, cfg, cpu_count=self._cpus)
        self.max_units = memory_ceiling(cfg, self._cpus, read_mem_total_mb())
        self._token = uuid.uuid4().hex
        self.leader_key = f"hc:{host_id}:leader"
        self.changed_key = f"hc:{host_id}:target_changed_at"

    async def _lead(self) -> bool:
        ttl_ms = max(3 * self._cfg.controller_interval_seconds, 10) * 1000
        return bool(await self._redis.raw.eval(LEAD_LUA, 1, self.leader_key, self._token, ttl_ms))

    async def tick(self) -> str:
        if not self._cfg.enabled:
            return "disabled"
        if not await self._lead():
            return "standby"
        raw = self._redis.raw
        snap = await self._admission.snapshot()
        cpu = self._read_cpu()
        if cpu is None:
            cpu = self._read_load(self._cpus)
        mem = self._read_mem()
        now = self._clock()
        changed_raw = await raw.get(self.changed_key)
        since_change = now - float(changed_raw) if changed_raw else float("inf")
        target = decide(
            snap.target,
            cpu_pressure=cpu,
            mem_available_mb=mem,
            waiters=snap.waiters,
            in_use=snap.in_use,
            since_change=since_change,
            cfg=self._cfg,
            max_units=self.max_units,
        )
        keep_ms = self._cfg.target_ttl_seconds * 1000
        if target != snap.target:
            direction = "up" if target > snap.target else "down"
            await raw.set(self.changed_key, str(now), px=keep_ms * 30)
            await raw.hincrby(self._admission.stats_key, f"adjust_{direction}", 1)
            logger.info(
                "host_capacity_target %s %.2f -> %.2f cpu_pressure=%s mem_available_mb=%s "
                "waiters=%d in_use=%.2f",
                direction,
                snap.target,
                target,
                cpu,
                mem,
                snap.waiters,
                snap.in_use,
            )
        # Rewritten every tick so its TTL only lapses when this loop stops.
        await raw.set(self._admission.target_key, str(target), px=keep_ms)
        await raw.hset(
            self._admission.stats_key,
            mapping={
                "cpu_pressure": "" if cpu is None else str(cpu),
                "mem_available_mb": "" if mem is None else str(mem),
            },
        )
        return (
            f"target={target:.2f} in_use={snap.in_use:.2f} waiters={snap.waiters} "
            f"cpu_pressure={cpu} mem_available_mb={mem}"
        )


async def run(config: AppConfig | None = None, stop: asyncio.Event | None = None) -> None:
    """Run the controller loop until a stop signal. Always runs — with
    host_capacity disabled each tick is a no-op — so its heartbeat, and so
    /v1/health, does not depend on the flag."""
    cfg = config or load_config()
    bootstrap_observability(cfg.observability)
    redis = RedisClient(redis_url=cfg.storage.redis_url)
    await redis.start()
    controller = CapacityController(redis, resolve_host_id(), cfg.host_capacity)
    task = asyncio.create_task(
        run_periodic(
            "capacity_control",
            controller.tick,
            cfg.host_capacity.controller_interval_seconds,
            redis=redis,
        )
    )
    logger.info(
        "capacity controller started (enabled=%s interval=%ss)",
        cfg.host_capacity.enabled,
        cfg.host_capacity.controller_interval_seconds,
    )
    external_stop = stop is not None
    stop = stop or asyncio.Event()
    if not external_stop:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            with contextlib.suppress(NotImplementedError):  # pragma: no cover
                loop.add_signal_handler(sig, stop.set)
    try:
        await stop.wait()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await redis.stop()
        logger.info("capacity controller stopped cleanly")


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":  # pragma: no cover — only true under `python -m`, not tests
    main()
