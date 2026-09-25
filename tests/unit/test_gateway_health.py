# tests/unit/test_gateway_health.py
"""proxy/gateway_health.py — the shared "gateway refuses our credentials"
verdict (round 68)."""

import json
import logging
from unittest.mock import AsyncMock

import pytest

from scraper_engine.proxy.gateway_health import REFUSED_KEY, GatewayHealth, GatewayRefusal


def _health(**raw_methods):
    redis = AsyncMock()
    for name, mock in raw_methods.items():
        setattr(redis.raw, name, mock)
    return GatewayHealth(redis, ttl_seconds=600), redis


class TestRefusal:
    @pytest.mark.asyncio
    async def test_no_key_means_not_refused(self):
        health, _ = _health(get=AsyncMock(return_value=None))
        assert await health.refusal() is None

    @pytest.mark.asyncio
    async def test_a_stored_record_is_returned(self):
        record = json.dumps({"since": "2026-09-25T11:20:00+00:00", "error": "407"})
        health, redis = _health(get=AsyncMock(return_value=record))
        assert await health.refusal() == GatewayRefusal(
            since="2026-09-25T11:20:00+00:00", error="407"
        )
        redis.raw.get.assert_awaited_once_with(REFUSED_KEY)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("raw", ["not json", '{"since": "x"}', "[1]"])
    async def test_an_unreadable_record_still_counts_as_refused(self, raw):
        health, _ = _health(get=AsyncMock(return_value=raw))
        refusal = await health.refusal()
        assert refusal is not None
        assert refusal.error == "unreadable refusal record"

    @pytest.mark.asyncio
    async def test_a_redis_failure_counts_as_not_refused(self):
        """Worst case one more refused attempt, which carries no traffic."""
        health, _ = _health(get=AsyncMock(side_effect=ConnectionError("down")))
        assert await health.refusal() is None


class TestMarkRefused:
    @pytest.mark.asyncio
    async def test_writes_once_with_the_ttl_and_logs(self, caplog):
        health, redis = _health(set=AsyncMock(return_value=True))
        with caplog.at_level(logging.WARNING, logger="scraper_engine.proxy.gateway_health"):
            await health.mark_refused("Page.goto: NS_ERROR_PROXY_AUTHENTICATION_FAILED")
        args, kwargs = redis.raw.set.await_args
        assert args[0] == REFUSED_KEY
        assert json.loads(args[1])["error"] == "Page.goto: NS_ERROR_PROXY_AUTHENTICATION_FAILED"
        assert kwargs == {"ex": 600, "nx": True}
        assert "paid_gateway_refused_credentials" in caplog.text

    @pytest.mark.asyncio
    async def test_an_existing_verdict_is_kept_and_not_logged_again(self, caplog):
        health, _ = _health(set=AsyncMock(return_value=None))
        with caplog.at_level(logging.WARNING, logger="scraper_engine.proxy.gateway_health"):
            await health.mark_refused("407")
        assert "paid_gateway_refused_credentials" not in caplog.text

    @pytest.mark.asyncio
    async def test_credentials_never_reach_the_record(self):
        health, redis = _health(set=AsyncMock(return_value=True))
        await health.mark_refused("proxy http://login__cr.ng:s3cret@gw.example:823 said 407")
        stored = json.loads(redis.raw.set.await_args.args[1])["error"]
        assert "s3cret" not in stored
        assert "login__cr.ng" not in stored
        assert "<credentials>@gw.example:823" in stored

    @pytest.mark.asyncio
    async def test_the_error_is_capped(self):
        health, redis = _health(set=AsyncMock(return_value=True))
        await health.mark_refused("x" * 5000)
        assert len(json.loads(redis.raw.set.await_args.args[1])["error"]) == 300

    @pytest.mark.asyncio
    async def test_a_redis_failure_is_swallowed(self):
        health, _ = _health(set=AsyncMock(side_effect=ConnectionError("down")))
        await health.mark_refused("407")


class TestClear:
    @pytest.mark.asyncio
    async def test_deletes_the_key(self):
        health, redis = _health(delete=AsyncMock())
        await health.clear()
        redis.raw.delete.assert_awaited_once_with(REFUSED_KEY)

    @pytest.mark.asyncio
    async def test_a_redis_failure_is_swallowed(self):
        health, _ = _health(delete=AsyncMock(side_effect=ConnectionError("down")))
        await health.clear()
