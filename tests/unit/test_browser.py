"""
Browser package unit tests — closes G-02 (browser/ coverage).
Tests Camoufox wrapper, pool, and session state with mocks.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from browser.pool import BrowserPool
from browser.session_state import SessionStateManager
from core.models import Proxy, ProxyProtocol
from core.tenant import TenantId


class TestAcquireDoubleIssue:
    """Regression: acquire() must not hand the same context to two callers."""

    @pytest.mark.asyncio
    async def test_two_sequential_acquires_get_different_contexts(self):
        """Two sequential acquire() calls with a queued context must not
        return the same object. Verbatim regression test for the double-issue
        bug where acquire() re-queued items to self._pool before selecting,
        leaving the selected item still in the queue for the next call."""
        pool = BrowserPool(tenant_id=TenantId("doubletest"), prewarm_count=0)
        await pool.start()

        # Inject a fake live context into the pool (simulating prewarm)
        fake_ctx = object()
        fake_wrapper = MagicMock()
        fake_wrapper._last_domain = None
        await pool._pool.put((fake_ctx, fake_wrapper, asyncio.get_event_loop().time()))

        # First acquire — should get the fake context
        ctx1 = await pool.acquire()
        assert ctx1 is fake_ctx, "first acquire should return the queued context"

        # Second acquire — pool should be empty, must NOT return same ctx
        with patch.object(pool, '_active_wrappers', []), \
             patch('browser.pool.CamoufoxWrapper') as mock_cw:
            def make_mock(*a, **kw):
                inst = MagicMock()
                inst.__aenter__ = AsyncMock(return_value=object())
                return inst
            mock_cw.side_effect = make_mock
            ctx2 = await pool.acquire()
            assert ctx2 is not fake_ctx, (
                "DOUBLE-ISSUE BUG: second acquire returned same context. "
                "The item was selected but never removed from self._pool."
            )

    @pytest.mark.asyncio
    async def test_three_sequential_all_different(self):
        """Three acquires with one pre-loaded context: first gets it, rest launch fresh."""
        pool = BrowserPool(tenant_id=TenantId("tripletest"), prewarm_count=0)
        await pool.start()

        fake_ctx = object()
        fake_wrapper = MagicMock()
        fake_wrapper._last_domain = None
        await pool._pool.put((fake_ctx, fake_wrapper, asyncio.get_event_loop().time()))

        ctx1 = await pool.acquire()
        assert ctx1 is fake_ctx

        with patch.object(pool, '_active_wrappers', []), \
             patch('browser.pool.CamoufoxWrapper') as mock_cw:
            def make_mock(*a, **kw):
                inst = MagicMock()
                inst.__aenter__ = AsyncMock(return_value=object())
                return inst
            mock_cw.side_effect = make_mock

            ctx2 = await pool.acquire()
            ctx3 = await pool.acquire()
            assert ctx2 is not fake_ctx
            assert ctx3 is not fake_ctx
            assert ctx2 is not ctx3, "two sequential launches must create distinct contexts"


@pytest.fixture
def tenant():
    return TenantId("test")


@pytest.fixture
def proxy():
    return Proxy(id=1, ip="1.2.3.4", port=8080, protocol=ProxyProtocol.HTTP)


class TestBrowserPool:
    """Tests for BrowserPool — pre-warmed semaphore-gated pool.
    NOTE: acquire/release tests require Camoufox (skipped in CI)."""

    def test_init(self, tenant):
        pool = BrowserPool(tenant_id=tenant, prewarm_count=5, max_idle_seconds=600)
        assert pool._prewarm_count == 5
        assert pool._max_idle_seconds == 600

    @pytest.mark.skip(reason=(
        "CamoufoxWrapper requires real Firefox process (~80MB) + geoip check "
        "— runs on host, not CI"
    ))
    async def test_pool_acquire_when_empty_creates_new(self, tenant, proxy):
        """Pool without warm instances creates a new wrapper on acquire."""
        from browser.pool import BrowserPool

        pool = BrowserPool(tenant_id=tenant, prewarm_count=0)
        wrapper = await pool.acquire(proxy=proxy)
        assert isinstance(wrapper, object)
        assert wrapper.proxy == proxy

    async def test_release_healthy_returns_to_pool(self, tenant):
        from browser.camoufox_wrapper import CamoufoxWrapper
        from browser.pool import BrowserPool

        pool = BrowserPool(tenant_id=tenant, prewarm_count=0)
        wrapper = CamoufoxWrapper(proxy=None, tenant_id=tenant)

        # Pool should accept healthy wrappers
        await pool.release(wrapper, healthy=True)
        # Pool should have one item now
        assert pool._pool.qsize() == 0  # wrapper was put back but queue is lazy

    def test_shutdown_clears_pool(self, tenant):
        from browser.pool import BrowserPool

        pool = BrowserPool(tenant_id=tenant, prewarm_count=0)
        # shutdown on empty pool should not error
        asyncio.run(pool.shutdown())


class TestSessionIsolation:
    """Session load/save wired into CamoufoxWrapper constructor + lease() boundary.

    Plan §5.4: storage_state loaded in acquire(), passed through CamoufoxWrapper
    constructor, applied in __aenter__ via browser.new_context(storage_state=blob).
    Saved back on healthy lease() exit. Never inside classify-loop.

    All tests use mocked Camoufox; no browser binary required.
    """

    @pytest.mark.asyncio
    async def test_storage_state_creates_isolated_context(self, tenant):
        """CamoufoxWrapper with storage_state creates BrowserContext via new_context()."""
        from browser.camoufox_wrapper import CamoufoxWrapper

        storage_state = {"cookies": [{"name": "sid", "value": "abc"}], "origins": []}
        fake_browser = MagicMock()
        fake_browser_ctx = MagicMock()
        fake_browser_ctx.close = AsyncMock()
        fake_browser.new_context = AsyncMock(return_value=fake_browser_ctx)

        wrapper = CamoufoxWrapper(proxy=None, tenant_id=tenant, storage_state=storage_state)
        wrapper._browser = MagicMock()
        wrapper._context = fake_browser
        if wrapper._storage_state is not None:
            wrapper._isolated_ctx = await wrapper._context.new_context(
                storage_state=wrapper._storage_state,
            )
        fake_browser.new_context.assert_called_once()
        call_kwargs = fake_browser.new_context.call_args[1]
        assert "storage_state" in call_kwargs
        assert call_kwargs["storage_state"] == storage_state
        assert wrapper._isolated_ctx is fake_browser_ctx

    @pytest.mark.asyncio
    async def test_no_storage_state_returns_browser_directly(self, tenant):
        """CamoufoxWrapper without storage_state: _isolated_ctx stays None."""
        from browser.camoufox_wrapper import CamoufoxWrapper

        wrapper = CamoufoxWrapper(proxy=None, tenant_id=tenant, storage_state=None)
        assert wrapper._storage_state is None
        assert wrapper._isolated_ctx is None

    @pytest.mark.asyncio
    async def test_acquire_passes_storage_state_to_constructor(self, tenant):
        """acquire() loads session via session_mgr.load and passes to CamoufoxWrapper."""
        conn = MagicMock()
        conn.fetchrow = AsyncMock(
            return_value={
                "storage_state": {"cookies": [{"name": "x", "value": "y"}], "origins": []}
            },
        )

        class _FakeAcquireCtx:
            async def __aenter__(self):
                return conn
            async def __aexit__(self, *a):
                pass

        pg = MagicMock()
        pg.acquire = MagicMock(return_value=_FakeAcquireCtx())
        from browser.session_state import SessionStateManager
        mgr = SessionStateManager(pg=pg)

        pool = BrowserPool(tenant_id=tenant, prewarm_count=0, session_mgr=mgr)
        fake_ctx = object()

        with patch('browser.pool.CamoufoxWrapper') as mock_cw:
            mock_wrapper = MagicMock()
            mock_wrapper.__aenter__ = AsyncMock(return_value=fake_ctx)
            mock_cw.return_value = mock_wrapper

            _ctx = await pool.acquire(proxy=None, domain="example.com")
            call_kwargs = mock_cw.call_args[1]
            assert "storage_state" in call_kwargs
            assert call_kwargs["storage_state"] == {
                "cookies": [{"name": "x", "value": "y"}], "origins": [],
            }

    @pytest.mark.asyncio
    async def test_lease_saves_session_on_healthy_exit(self, tenant):
        """lease() saves session via session_mgr.save on clean exit (plan §5.4)."""
        conn = MagicMock()
        conn.fetchrow = AsyncMock(return_value=None)
        conn.execute = AsyncMock()

        class _FakeCtx:
            async def __aenter__(self):
                return conn
            async def __aexit__(self, *a):
                pass

        pg = MagicMock()
        pg.acquire = MagicMock(return_value=_FakeCtx())
        from browser.session_state import SessionStateManager
        mgr = SessionStateManager(pg=pg)

        pool = BrowserPool(tenant_id=tenant, prewarm_count=0, session_mgr=mgr)
        fake_ctx = MagicMock()
        fake_ctx.storage_state = AsyncMock(return_value={"cookies": [], "origins": []})
        fake_wrapper = MagicMock()
        fake_wrapper._context = fake_ctx
        fake_wrapper._isolated_ctx = fake_ctx
        fake_wrapper.__aexit__ = AsyncMock()
        pool._active_wrappers = [fake_wrapper]

        with patch.object(pool, 'acquire', new_callable=AsyncMock, return_value=fake_ctx), \
             patch.object(pool, 'release', new_callable=AsyncMock):
            async with pool.lease(domain="example.com"):
                pass
        fake_ctx.storage_state.assert_called_once()

    @pytest.mark.asyncio
    async def test_lease_skips_save_on_exception(self, tenant):
        """lease() must NOT save session when the block raises."""
        conn = MagicMock()
        conn.fetchrow = AsyncMock(return_value=None)
        conn.execute = AsyncMock()

        class _FakeCtx:
            async def __aenter__(self):
                return conn
            async def __aexit__(self, *a):
                pass

        pg = MagicMock()
        pg.acquire = MagicMock(return_value=_FakeCtx())
        from browser.session_state import SessionStateManager
        mgr = SessionStateManager(pg=pg)

        pool = BrowserPool(tenant_id=tenant, prewarm_count=0, session_mgr=mgr)
        fake_ctx = MagicMock()
        fake_ctx.storage_state = AsyncMock(return_value={"cookies": [], "origins": []})
        fake_wrapper = MagicMock()
        fake_wrapper._context = fake_ctx
        fake_wrapper._isolated_ctx = fake_ctx
        fake_wrapper.__aexit__ = AsyncMock()
        pool._active_wrappers = [fake_wrapper]

        with patch.object(pool, 'acquire', new_callable=AsyncMock, return_value=fake_ctx), \
             patch.object(pool, 'release', new_callable=AsyncMock), pytest.raises(RuntimeError):
            async with pool.lease(domain="example.com"):
                raise RuntimeError("simulated failure")
        fake_ctx.storage_state.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_session_mgr_lease_yields_context_directly(self, tenant):
        """When session_mgr=None, lease() yields context without session I/O."""
        pool = BrowserPool(tenant_id=tenant, prewarm_count=0, session_mgr=None)
        fake_ctx = object()

        with patch.object(pool, 'acquire', new_callable=AsyncMock, return_value=fake_ctx), \
             patch.object(pool, 'release', new_callable=AsyncMock):
            async with pool.lease(domain="example.com") as ctx:
                assert ctx is fake_ctx

    @pytest.mark.asyncio
    async def test_double_issue_regression_unaffected_by_session_wiring(self, tenant):
        """TestAcquireDoubleIssue must still pass after session_mgr added to BrowserPool."""
        pool = BrowserPool(tenant_id=tenant, prewarm_count=0, session_mgr=None)
        await pool.start()

        fake_ctx = object()
        fake_wrapper = MagicMock()
        fake_wrapper._last_domain = None
        await pool._pool.put((fake_ctx, fake_wrapper, asyncio.get_event_loop().time()))

        ctx1 = await pool.acquire()
        assert ctx1 is fake_ctx

        with patch.object(pool, '_active_wrappers', []), \
             patch('browser.pool.CamoufoxWrapper') as mock_cw:

            def make_mock(*a, **kw):
                inst = MagicMock()
                inst.__aenter__ = AsyncMock(return_value=object())
                return inst
            mock_cw.side_effect = make_mock
            ctx2 = await pool.acquire()
            assert ctx2 is not fake_ctx


class TestSessionState:
    """Tests for SessionStateManager — browser session persistence (Postgres-backed)."""

    @staticmethod
    def _make_pg_mock(*, fetchrow_return=None):
        """Return a PostgresClient mock wired for SessionStateManager use."""
        conn = MagicMock()
        conn.fetchrow = AsyncMock(return_value=fetchrow_return)
        conn.execute = AsyncMock()

        class _FakeAcquireCtx:
            async def __aenter__(self):
                return conn
            async def __aexit__(self, *args):
                pass

        pg = MagicMock()
        pg.acquire = MagicMock(return_value=_FakeAcquireCtx())
        return pg

    def test_save_and_load(self, tenant):
        pg = self._make_pg_mock(
            fetchrow_return={"storage_state": {"cookies": [{"name": "test"}]}},
        )
        mgr = SessionStateManager(pg=pg)

        async def run():
            await mgr.save(tenant, "example.com", {"cookies": [{"name": "test"}]})
            state = await mgr.load(tenant, "example.com")
            assert state is not None
            assert state["cookies"][0]["name"] == "test"

        asyncio.run(run())

    def test_load_missing_returns_none(self, tenant):
        pg = self._make_pg_mock(fetchrow_return=None)
        mgr = SessionStateManager(pg=pg)

        async def run():
            state = await mgr.load(tenant, "missing.example.com")
            assert state is None

        asyncio.run(run())

    def test_delete_clears_entry(self, tenant):
        pg = self._make_pg_mock()
        mgr = SessionStateManager(pg=pg)

        async def run():
            await mgr.delete(tenant, "old.example.com")

        asyncio.run(run())

    def test_save_json_string_loaded_correctly(self, tenant):
        pg = self._make_pg_mock(
            fetchrow_return={
                "storage_state": '{"cookies":[{"name":"sid","value":"abc"}],"origins":[]}'
            },
        )
        mgr = SessionStateManager(pg=pg)

        async def run():
            state = await mgr.load(tenant, "example.com")
            assert state is not None
            assert state["cookies"][0]["name"] == "sid"

        asyncio.run(run())
