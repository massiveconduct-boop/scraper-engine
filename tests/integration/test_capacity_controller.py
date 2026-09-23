# tests/integration/test_capacity_controller.py
"""orchestrator/capacity_controller.py (round 65): pressure readers, the
AIMD decision, and the leader-locked tick against REAL Redis."""

from __future__ import annotations

import asyncio
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from scraper_engine.config.schema import AppConfig, HostCapacityConfig
from scraper_engine.orchestrator import capacity_controller as cc
from scraper_engine.orchestrator.capacity_controller import CapacityController, decide
from scraper_engine.storage.redis_client import RedisClient

pytestmark = pytest.mark.integration

CFG = HostCapacityConfig(enabled=True, min_units=1, max_units=8)


class TestReaders:
    def test_cpu_pressure_reads_some_avg10(self, tmp_path):
        psi = tmp_path / "cpu"
        psi.write_text(
            "some avg10=12.50 avg60=3.00 avg300=1.00 total=1\n"
            "full avg10=0.00 avg60=0.00 avg300=0.00 total=0\n"
        )
        assert cc.read_cpu_pressure(psi) == 12.5

    def test_cpu_pressure_missing_file_or_field(self, tmp_path):
        assert cc.read_cpu_pressure(tmp_path / "absent") is None
        odd = tmp_path / "odd"
        odd.write_text("some avg60=1.0\nfull avg10=2.0\n")
        assert cc.read_cpu_pressure(odd) is None

    def test_real_host_pressure_is_readable(self):
        assert cc.read_cpu_pressure() is not None
        assert cc.read_mem_available_mb() > 0

    def test_load_pressure_scale(self):
        assert cc.load_pressure(4, lambda: (2.0, 0, 0)) == 0.0
        assert cc.load_pressure(4, lambda: (5.6, 0, 0)) == pytest.approx(40.0)
        assert cc.load_pressure(4, lambda: (7.2, 0, 0)) == pytest.approx(80.0)

        def broken():
            raise OSError("no loadavg")

        assert cc.load_pressure(4, broken) is None
        assert cc.load_pressure(4) is not None

    def test_mem_available(self, tmp_path):
        info = tmp_path / "meminfo"
        info.write_text("MemTotal: 100 kB\nMemAvailable: 2097152 kB\n")
        assert cc.read_mem_available_mb(info) == 2048
        assert cc.read_mem_available_mb(tmp_path / "absent") is None
        (tmp_path / "none").write_text("MemTotal: 100 kB\n")
        assert cc.read_mem_available_mb(tmp_path / "none") is None

    def test_mem_total(self, tmp_path):
        info = tmp_path / "meminfo"
        info.write_text("MemTotal: 24576000 kB\nMemAvailable: 100 kB\n")
        assert cc.read_mem_total_mb(info) == 24000
        assert cc.read_mem_total_mb(tmp_path / "absent") is None
        (tmp_path / "none").write_text("MemAvailable: 100 kB\n")
        assert cc.read_mem_total_mb(tmp_path / "none") is None
        assert cc.read_mem_total_mb() > 0


class TestDecide:
    def _d(
        self, current=4.0, cpu=50.0, mem=40000, waiters=1, in_use=4.0, since=1000.0, max_units=8.0
    ):
        return decide(
            current,
            cpu_pressure=cpu,
            mem_available_mb=mem,
            waiters=waiters,
            in_use=in_use,
            since_change=since,
            cfg=CFG,
            max_units=max_units,
        )

    def test_raise_only_when_calm_and_someone_waits(self):
        assert self._d(cpu=10.0) == 5.0
        assert self._d(cpu=10.0, waiters=0) == 4.0
        assert self._d(cpu=10.0, since=5.0) == 4.0  # inside raise dwell
        assert self._d(cpu=10.0, current=8.0) == 8.0  # at max

    def test_a_calm_host_admits_the_whole_queue_in_one_step(self):
        """Round 66 — not one unit per dwell: short jobs finished before the
        target caught up, so on light pages the limiter only added waiting."""
        assert self._d(cpu=10.0, waiters=3, max_units=20.0) == 7.0
        assert self._d(cpu=10.0, waiters=30) == 8.0  # still capped

    def test_a_raise_never_outgrows_free_memory(self):
        # 1536 MB floor + 2 x 1200 MB spare -> room for 2 more browsers only.
        assert self._d(cpu=10.0, waiters=5, mem=1536 + 2400, max_units=20.0) == 6.0
        # No spare memory: hold, never cut while calm.
        assert self._d(cpu=10.0, waiters=5, mem=1600, max_units=20.0) == 4.0
        # No memory reading: only the queue and the ceiling bound the raise.
        assert self._d(cpu=10.0, waiters=5, mem=None, max_units=20.0) == 9.0

    def test_waiters_already_covered_by_the_target_raise_nothing(self):
        """Seats are free but the waiter is queued for something else (its
        site's politeness slot): a bigger target would not help it."""
        assert self._d(cpu=10.0, waiters=1, in_use=2.0) == 4.0

    def test_memory_ceiling(self):
        assert cc.memory_ceiling(CFG, 4, 24000) == 8.0  # explicit max wins
        auto = HostCapacityConfig(enabled=True)
        assert cc.memory_ceiling(auto, 4, 24000) == 20.0  # 24000 // 1200
        assert cc.memory_ceiling(auto, 4, None) == 8.0  # no reading: 2 x CPUs
        assert cc.memory_ceiling(auto, 4, 500) == 1.0  # never below min_units

    def test_cut_on_cpu_or_memory_strain(self):
        assert self._d(cpu=95.0) == 2.8
        assert self._d(mem=100) == 2.8
        assert self._d(cpu=95.0, since=5.0) == 4.0  # inside cut dwell
        assert self._d(cpu=95.0, current=1.2) == 1.0  # floor
        assert self._d(cpu=85.0, waiters=0) == 4.0  # 85 is busy, not strained

    def test_between_the_marks_it_keeps_climbing_a_step_at_a_time(self):
        """Round 66 — it used to hold here, so after any cut it never came back
        (live: 2-4 browsers on an idle host, pages queued for minutes)."""
        assert self._d(cpu=60.0) == 5.0  # one unit minimum
        assert self._d(cpu=60.0, current=8.0, in_use=8.0, waiters=10, max_units=20.0) == 10.0
        assert self._d(cpu=60.0, waiters=0) == 4.0  # nobody waiting: hold

    def test_hold_without_signal(self):
        assert self._d(cpu=None, mem=None) == 4.0

    def test_out_of_bounds_current_is_clamped(self):
        assert self._d(current=20.0, waiters=0) == 8.0
        assert self._d(current=0.1, waiters=0) == 1.0


@pytest.fixture
async def redis():
    client = RedisClient(redis_url="redis://localhost:6379/0")
    await client.start()
    yield client
    keys = [k async for k in client.raw.scan_iter(match="hc:cctest-*", count=1000)]
    if keys:
        await client.raw.delete(*keys)
    await client.stop()


def _controller(redis, host, cfg=CFG, cpu=10.0, mem=4000, load=None, clock=None):
    return CapacityController(
        redis,
        host,
        cfg,
        cpu_count=4,
        clock=clock or (lambda: 1_000_000.0),
        cpu_pressure_reader=lambda: cpu,
        mem_reader=lambda: mem,
        load_reader=lambda n: load,
    )


class TestTick:
    @pytest.mark.asyncio
    async def test_disabled_is_a_no_op(self, redis):
        host = f"cctest-{uuid.uuid4().hex[:6]}"
        ctl = _controller(redis, host, cfg=HostCapacityConfig(enabled=False))
        assert await ctl.tick() == "disabled"
        assert await redis.raw.get(f"hc:{host}:target") is None

    @pytest.mark.asyncio
    async def test_leader_writes_target_and_holds_without_waiters(self, redis):
        host = f"cctest-{uuid.uuid4().hex[:6]}"
        ctl = _controller(redis, host)
        summary = await ctl.tick()
        assert summary.startswith("target=4.00")  # default = cpu count
        assert await redis.raw.get(f"hc:{host}:target") == "4.0"
        assert await redis.raw.pttl(f"hc:{host}:target") > 0
        stats = await redis.raw.hgetall(f"hc:{host}:stats")
        assert stats["cpu_pressure"] == "10.0" and stats["mem_available_mb"] == "4000"

    @pytest.mark.asyncio
    async def test_a_second_controller_stands_by(self, redis):
        host = f"cctest-{uuid.uuid4().hex[:6]}"
        assert (await _controller(redis, host).tick()).startswith("target=")
        assert await _controller(redis, host).tick() == "standby"

    @pytest.mark.asyncio
    async def test_raises_with_waiters_then_respects_the_dwell(self, redis):
        host = f"cctest-{uuid.uuid4().hex[:6]}"
        now = [1_000_000.0]
        ctl = _controller(redis, host, clock=lambda: now[0], mem=40000)
        await redis.raw.zadd(f"hc:{host}:waiter_exp", {f"w{i}": 10**13 for i in range(6)})
        await ctl.tick()
        # Nothing in use, 6 waiting: straight to 6 (was one unit per dwell).
        assert await redis.raw.get(f"hc:{host}:target") == "6.0"
        assert await redis.raw.hget(f"hc:{host}:stats", "adjust_up") == "1"
        await redis.raw.zadd(f"hc:{host}:waiter_exp", {"w6": 10**13, "w7": 10**13})
        now[0] += 5
        await ctl.tick()
        assert await redis.raw.get(f"hc:{host}:target") == "6.0"  # inside the dwell
        now[0] += 60
        await ctl.tick()
        assert await redis.raw.get(f"hc:{host}:target") == "8.0"

    @pytest.mark.asyncio
    async def test_cuts_under_strain_using_the_load_fallback(self, redis):
        host = f"cctest-{uuid.uuid4().hex[:6]}"
        ctl = _controller(redis, host, cpu=None, mem=None, load=95.0)
        await ctl.tick()
        assert await redis.raw.get(f"hc:{host}:target") == "2.8"
        assert await redis.raw.hget(f"hc:{host}:stats", "adjust_down") == "1"
        stats = await redis.raw.hgetall(f"hc:{host}:stats")
        assert stats["cpu_pressure"] == "95.0" and stats["mem_available_mb"] == ""

    @pytest.mark.asyncio
    async def test_no_pressure_signal_at_all_holds(self, redis):
        host = f"cctest-{uuid.uuid4().hex[:6]}"
        ctl = _controller(redis, host, cpu=None, mem=None, load=None)
        await ctl.tick()
        assert await redis.raw.hget(f"hc:{host}:stats", "cpu_pressure") == ""


class TestRun:
    @pytest.mark.asyncio
    async def test_run_ticks_and_stops_cleanly(self, monkeypatch):
        host = f"cctest-{uuid.uuid4().hex[:6]}"
        monkeypatch.setenv("SCRAPER_HOST_ID", host)
        monkeypatch.setattr(cc, "bootstrap_observability", MagicMock())
        cfg = AppConfig(host_capacity=HostCapacityConfig(enabled=True))
        cfg.storage.redis_url = "redis://localhost:6379/0"
        stop = asyncio.Event()
        task = asyncio.create_task(cc.run(cfg, stop=stop))
        client = RedisClient(redis_url="redis://localhost:6379/0")
        await client.start()
        try:
            for _ in range(50):
                if await client.raw.get(f"hc:{host}:target"):
                    break
                await asyncio.sleep(0.05)
            assert await client.raw.get(f"hc:{host}:target") is not None
            assert await client.raw.get("heartbeat:capacity_control") is not None
        finally:
            stop.set()
            await task
            keys = [k async for k in client.raw.scan_iter(match=f"hc:{host}:*")]
            if keys:
                await client.raw.delete(*keys)
            await client.stop()

    @pytest.mark.asyncio
    async def test_installs_real_signal_handlers_when_stop_not_supplied(self, monkeypatch):
        import os
        import signal as signal_module

        redis = MagicMock()
        redis.start = AsyncMock()
        redis.stop = AsyncMock()
        monkeypatch.setattr(cc, "RedisClient", MagicMock(return_value=redis))
        monkeypatch.setattr(cc, "bootstrap_observability", MagicMock())
        monkeypatch.setattr(cc, "run_periodic", AsyncMock())
        task = asyncio.create_task(cc.run(config=AppConfig()))
        await asyncio.sleep(0.1)  # let run() reach add_signal_handler before we fire one
        os.kill(os.getpid(), signal_module.SIGTERM)
        await asyncio.wait_for(task, timeout=5)
        redis.stop.assert_awaited_once()

    def test_main_runs_the_loop(self, monkeypatch):
        seen = {}

        async def fake_run():
            seen["ran"] = True

        monkeypatch.setattr(cc, "run", fake_run)
        cc.main()
        assert seen == {"ran": True}


class TestMeasuredBrowserMemory:
    """Round 67 — the host is sized by what its browsers were measured to
    weigh (core/browser_rss.py), not by the 1200 MB constant. On this
    project's own host the constant is what bound: 10.5 GB MemAvailable held
    the target near 9 browsers while an unlimited run drove 14-21 of them at
    CPU PSI 15-19."""

    def test_a_raise_is_charged_at_what_a_browser_actually_weighs(self):
        # 1536 MB floor + 2400 MB spare: 2 browsers at the constant, 4 at the
        # 600 MB these browsers were measured to use.
        assert (
            decide(
                4.0,
                cpu_pressure=10.0,
                mem_available_mb=1536 + 2400,
                waiters=5,
                in_use=4.0,
                since_change=1000.0,
                cfg=CFG,
                max_units=20.0,
                browser_mb=600.0,
            )
            == 8.0
        )

    def test_the_ceiling_moves_with_the_measurement(self):
        auto = HostCapacityConfig(enabled=True)
        assert cc.memory_ceiling(auto, 4, 24000, 600.0) == 40.0
        assert cc.memory_ceiling(auto, 4, 24000, None) == 20.0

    @pytest.mark.asyncio
    async def test_tick_sizes_the_host_from_what_the_workers_reported(self, redis):
        from scraper_engine.core.browser_rss import BrowserMemorySample

        host = f"cctest-{uuid.uuid4().hex[:6]}"
        cfg = HostCapacityConfig(enabled=True, min_browser_memory_mb=100, default_units=1.0)
        ctl = _controller(redis, host, cfg=cfg, mem=1536 + 2400)
        await redis.raw.zadd(f"hc:{host}:waiter_exp", {f"w{i}": 10**13 for i in range(6)})

        # No reports yet: the configured constant, two browsers' worth of spare.
        assert (await ctl.tick()).endswith("browser_mb=1200")
        assert await redis.raw.get(f"hc:{host}:target") == "2.0"

        await ctl._admission.report_browser_memory(
            "w1", BrowserMemorySample(count=4, mean_mb=600.0)
        )
        await redis.raw.delete(f"hc:{host}:target_changed_at")

        assert (await ctl.tick()).endswith("browser_mb=600")
        # Same free memory, half the weight per browser: twice the room.
        assert await redis.raw.get(f"hc:{host}:target") == "4.0"
        assert await redis.raw.hget(f"hc:{host}:stats", "browser_mb") == "600.0"
