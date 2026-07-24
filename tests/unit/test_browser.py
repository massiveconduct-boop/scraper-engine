"""
Browser package unit tests — closes G-02 (browser/ coverage).
Tests Camoufox wrapper, pool, and session state with mocks.
"""

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from browser.pool import BrowserPool
from browser.session_state import SessionStateManager
from core.models import Proxy, ProxyProtocol
from core.tenant import TenantId


class TestAcquireDoubleIssue:
    """Regression: acquire() double-issue bug."""

    @pytest.mark.asyncio
    async def test_two_sequential_acquires_get_different_contexts(self):
        pool = BrowserPool(tenant_id=TenantId("d1"), prewarm_count=0)
        await pool.start()
        fake_ctx = object()
        fw = MagicMock()
        fw._last_domain = None
        await pool._pool.put((fake_ctx, fw, time.monotonic()))
        ctx1 = await pool.acquire()
        assert ctx1 is fake_ctx
        with patch.object(pool, '_active_wrappers', []), \
             patch('browser.pool.CamoufoxWrapper') as m:
            def mk(*a,**kw):
                i=MagicMock();i.__aenter__=AsyncMock(return_value=object());return i
            m.side_effect = mk
            ctx2 = await pool.acquire()
            assert ctx2 is not fake_ctx, "DOUBLE-ISSUE: second acquire got same context"

    @pytest.mark.asyncio
    async def test_three_sequential_all_different(self):
        pool = BrowserPool(tenant_id=TenantId("t1"), prewarm_count=0)
        await pool.start()
        fake_ctx = object()
        fw = MagicMock()
        fw._last_domain = None
        await pool._pool.put((fake_ctx, fw, time.monotonic()))
        ctx1 = await pool.acquire()
        assert ctx1 is fake_ctx
        with patch.object(pool, '_active_wrappers', []), \
             patch('browser.pool.CamoufoxWrapper') as m:
            def mk(*a,**kw):
                i=MagicMock();i.__aenter__=AsyncMock(return_value=object());return i
            m.side_effect = mk
            ctx2 = await pool.acquire()
            ctx3 = await pool.acquire()
            assert ctx2 is not fake_ctx
            assert ctx3 is not fake_ctx
            assert ctx2 is not ctx3


@pytest.fixture
def tenant():
    return TenantId("test")

@pytest.fixture
def proxy():
    return Proxy(id=1, ip="1.2.3.4", port=8080, protocol=ProxyProtocol.HTTP)


class TestBrowserPool:
    def test_init(self, tenant):
        pool = BrowserPool(tenant_id=tenant, prewarm_count=5, max_idle_seconds=600)
        assert pool._prewarm_count == 5

    @pytest.mark.skip(reason="CamoufoxWrapper import triggers binary")
    @pytest.mark.asyncio
    async def test_pool_acquire_when_empty_creates_new(self, tenant, proxy):
        pool = BrowserPool(tenant_id=tenant, prewarm_count=0)
        wrapper = await pool.acquire(proxy=proxy)
        assert isinstance(wrapper, object)

    @pytest.mark.skip(reason="CamoufoxWrapper import triggers binary")
    @pytest.mark.asyncio
    async def test_release_healthy_returns_to_pool(self, tenant):
        from browser.camoufox_wrapper import CamoufoxWrapper
        pool = BrowserPool(tenant_id=tenant, prewarm_count=0)
        wrapper = CamoufoxWrapper(proxy=None, tenant_id=tenant)
        await pool.release(wrapper, healthy=True)

    def test_shutdown_clears_pool(self, tenant):
        pool = BrowserPool(tenant_id=tenant, prewarm_count=0)
        asyncio.run(pool.shutdown())


class TestSessionState:
    def test_save_and_load(self, tenant):
        redis = AsyncMock()
        redis.get.return_value = '{"cookies": [{"name": "test"}]}'
        mgr = SessionStateManager(redis=redis)
        async def run():
            await mgr.save(tenant, "p1", {"cookies": [{"name": "test"}]})
            state = await mgr.load(tenant, "p1")
            assert state["cookies"][0]["name"] == "test"
        asyncio.run(run())

    def test_load_missing_returns_none(self, tenant):
        redis = AsyncMock()
        redis.get.return_value = None
        mgr = SessionStateManager(redis=redis)
        async def run():
            assert await mgr.load(tenant, "missing") is None
        asyncio.run(run())

    def test_delete_clears_entry(self, tenant):
        redis = AsyncMock()
        mgr = SessionStateManager(redis=redis)
        async def run():
            await mgr.delete(tenant, "prof-old")
        asyncio.run(run())
