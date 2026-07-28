# tests/unit/test_harvester_daemon.py
"""Proxy-harvester daemon wiring (proxy/harvester_daemon.py).

The daemon supervises three background routines on independent timers. These
tests use fake clients/routines (no DB, no Redis, no network) to verify the
supervisor's contract: it builds everything from config, isolates a failing
cycle so the loop survives, and shuts down cleanly. Mirrors the fake-client
style in test_captcha_inpage.py."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

import scraper_engine.proxy.harvester_daemon as mod


class TestRunPeriodic:
    @pytest.mark.asyncio
    async def test_runs_cycle_then_propagates_cancel(self, monkeypatch):
        cycle = AsyncMock(return_value="ok")

        async def fake_sleep(_):  # break out of the infinite loop after one cycle
            raise asyncio.CancelledError

        monkeypatch.setattr(mod.asyncio, "sleep", fake_sleep)
        with pytest.raises(asyncio.CancelledError):
            await mod._run_periodic("harvest", cycle, 600)
        cycle.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_swallows_cycle_error_and_keeps_looping(self, monkeypatch):
        calls = {"n": 0}

        async def cycle():
            calls["n"] += 1
            raise RuntimeError("transient boom")

        async def fake_sleep(_):
            raise asyncio.CancelledError

        monkeypatch.setattr(mod.asyncio, "sleep", fake_sleep)
        # RuntimeError must NOT escape — only the CancelledError that stops the loop.
        with pytest.raises(asyncio.CancelledError):
            await mod._run_periodic("harvest", cycle, 600)
        assert calls["n"] == 1  # ran despite raising; error was swallowed


class TestRun:
    @pytest.mark.asyncio
    async def test_wires_from_config_and_shuts_down_cleanly(self, monkeypatch):
        pg = AsyncMock()
        redis = AsyncMock()
        pg_cls = MagicMock(return_value=pg)
        redis_cls = MagicMock(return_value=redis)
        monkeypatch.setattr(mod, "PostgresClient", pg_cls)
        monkeypatch.setattr(mod, "RedisClient", redis_cls)

        harv = MagicMock(harvest_once=AsyncMock(return_value=0))
        promo = MagicMock(run_once=AsyncMock(return_value={}))
        health = MagicMock(check_all=AsyncMock(return_value={}))
        harv_cls = MagicMock(return_value=harv)
        monkeypatch.setattr(mod, "ProxyHarvester", harv_cls)
        monkeypatch.setattr(mod, "ProxyPromotionJob", MagicMock(return_value=promo))
        monkeypatch.setattr(mod, "HealthMonitor", MagicMock(return_value=health))

        from scraper_engine.config.schema import AppConfig

        stop = asyncio.Event()
        stop.set()  # request shutdown immediately — exercise start + clean teardown
        await mod.run(config=AppConfig(), stop=stop)

        # DB + Redis both started from the single config source, and stopped.
        pg.start.assert_awaited_once()
        redis.start.assert_awaited_once()
        pg.stop.assert_awaited_once()
        redis.stop.assert_awaited_once()
        # DSN came from StorageConfig, not a hardcoded string.
        assert pg_cls.call_args.args[0] == AppConfig().storage.database_url
        # Harvester built with the configured source list.
        assert harv_cls.call_args.kwargs["sources"] == AppConfig().proxy_harvester.sources
