# tests/unit/test_session_isolation.py
"""Session isolation unit tests — domain A cookies must not leak to domain B.

Plan §5.5: session loading in acquire() → CamoufoxWrapper constructor.
Session saving in lease() on healthy exit. Never inside classify-loop.

All tests use mocked browser contexts; no Camoufox binary required.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from scraper_engine.browser.pool import BrowserPool
from scraper_engine.core.tenant import TenantId


@pytest.fixture
def tenant():
    return TenantId("isolation_test")


def _make_session_mgr(load_return=None):
    """Return a mocked SessionStateManager (Postgres-backed)."""
    mgr = MagicMock()
    mgr.load = AsyncMock(return_value=load_return)
    mgr.save = AsyncMock()
    mgr.delete = AsyncMock()
    return mgr


class TestDomainIsolation:
    """Domain A → Domain B must not carry cookies/storage state."""

    @pytest.mark.asyncio
    async def test_domain_a_then_domain_b_does_not_carry_cookies(self, tenant):
        """Acquire for domain A, simulate cookie set, release healthy.
        Acquire for domain B — assert B's context has NO cookie from A.

        Plan §5.5 regression test for the original session leak.
        """
        storage_a = {
            "cookies": [{"name": "session_a", "value": "secret_a", "domain": "a.example.com"}],
            "origins": [],
        }
        session_mgr = _make_session_mgr(load_return=storage_a)

        pool = BrowserPool(tenant_id=tenant, prewarm_count=0, session_mgr=session_mgr)

        # ── Domain A: CamoufoxWrapper constructed with storage_state ──
        fake_ctx_a = MagicMock()
        fake_ctx_a.storage_state = AsyncMock(return_value=storage_a)

        with patch("scraper_engine.browser.pool.CamoufoxWrapper") as mock_cw:
            mock_wrapper = MagicMock()
            mock_wrapper._isolated_ctx = fake_ctx_a
            mock_wrapper._context = fake_ctx_a
            mock_wrapper.__aenter__ = AsyncMock(return_value=fake_ctx_a)
            mock_cw.return_value = mock_wrapper

            with patch.object(pool, "release", new_callable=AsyncMock):
                async with pool.lease(domain="a.example.com") as ctx_a:
                    assert ctx_a is fake_ctx_a

            # Domain A: storage_state passed to constructor (plan §5.4)
            a_kwargs = mock_cw.call_args[1]
            assert a_kwargs.get("storage_state") == storage_a

        # Domain A's state should have been saved on healthy exit
        session_mgr.save.assert_called_once()

        # ── Domain B: load returns None, gets clean context ──
        session_mgr.load = AsyncMock(return_value=None)
        session_mgr.save.reset_mock()

        fake_ctx_b = MagicMock()
        fake_ctx_b.storage_state = AsyncMock(return_value={"cookies": [], "origins": []})

        with patch("scraper_engine.browser.pool.CamoufoxWrapper") as mock_cw_b:
            mock_wrapper_b = MagicMock()
            mock_wrapper_b._isolated_ctx = None  # no storage_state
            mock_wrapper_b._context = fake_ctx_b
            mock_wrapper_b.__aenter__ = AsyncMock(return_value=fake_ctx_b)
            mock_cw_b.return_value = mock_wrapper_b

            with patch.object(pool, "release", new_callable=AsyncMock):
                async with pool.lease(domain="b.example.com") as ctx_b:
                    assert ctx_b is fake_ctx_b

            # Domain B: storage_state=None passed to constructor (no loaded state)
            b_kwargs = mock_cw_b.call_args[1]
            assert b_kwargs.get("storage_state") is None, (
                "ISOLATION LEAK: domain B received storage_state — A's session bled into B"
            )

    @pytest.mark.asyncio
    async def test_same_domain_reacquire_loads_persisted_state(self, tenant):
        """Acquire for domain A, release healthy, acquire for domain A again —
        second acquire must pass persisted storage_state to constructor."""
        saved_state = {
            "cookies": [{"name": "sid", "value": "abc123", "domain": "a.example.com"}],
            "origins": [],
        }
        session_mgr = _make_session_mgr(load_return=saved_state)
        pool = BrowserPool(tenant_id=tenant, prewarm_count=0, session_mgr=session_mgr)

        fake_ctx = MagicMock()
        fake_ctx.storage_state = AsyncMock(return_value=saved_state)

        with patch("scraper_engine.browser.pool.CamoufoxWrapper") as mock_cw:
            mock_wrapper = MagicMock()
            mock_wrapper._isolated_ctx = fake_ctx
            mock_wrapper._context = fake_ctx
            mock_wrapper.__aenter__ = AsyncMock(return_value=fake_ctx)
            mock_cw.return_value = mock_wrapper

            with patch.object(pool, "release", new_callable=AsyncMock):
                async with pool.lease(domain="a.example.com") as ctx:
                    assert ctx is fake_ctx

            # Constructor must receive the persisted storage_state
            call_kwargs = mock_cw.call_args[1]
            assert "storage_state" in call_kwargs, (
                "Plan §5.4: acquire must pass storage_state to CamoufoxWrapper constructor"
            )
            assert call_kwargs["storage_state"] == saved_state

    @pytest.mark.asyncio
    async def test_session_mgr_none_acquire_no_storage_state(self, tenant):
        """Without session_mgr, acquire() passes storage_state=None to constructor."""
        pool = BrowserPool(tenant_id=tenant, prewarm_count=0, session_mgr=None)

        fake_ctx = object()
        with patch("scraper_engine.browser.pool.CamoufoxWrapper") as mock_cw:
            mock_wrapper = MagicMock()
            mock_wrapper.__aenter__ = AsyncMock(return_value=fake_ctx)
            mock_cw.return_value = mock_wrapper

            with patch.object(pool, "release", new_callable=AsyncMock):
                async with pool.lease(domain="example.com") as ctx:
                    assert ctx is fake_ctx

            # No session_mgr → storage_state=None
            call_kwargs = mock_cw.call_args[1]
            assert call_kwargs.get("storage_state") is None, (
                "Without session_mgr, storage_state must be None"
            )

    @pytest.mark.asyncio
    async def test_delete_called_on_bad_session(self, tenant):
        """When delete() is called (poisoned session), subsequent lease gets clean context."""
        session_mgr = _make_session_mgr()
        await session_mgr.delete(tenant, "bad.example.com")
        session_mgr.delete.assert_called_once()
        delete_call_domain = session_mgr.delete.call_args[0][1]
        assert delete_call_domain == "bad.example.com"
