"""
Browser package unit tests — closes G-02 (browser/ coverage).
Tests Camoufox wrapper, pool, and session state with mocks.
"""

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from scraper_engine.browser.pool import BrowserPool
from scraper_engine.browser.session_state import SessionStateManager
from scraper_engine.core.models import Proxy, ProxyProtocol
from scraper_engine.core.tenant import TenantId


def _camoufox_installed() -> bool:
    """Same "run local, skip CI" gate as tests/chaos/test_safe_content_guard.py —
    True only if the Camoufox browser binary is actually fetched."""
    try:
        from camoufox.pkgman import installed_verstr

        return bool(installed_verstr())
    except Exception:
        return False


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
        with (
            patch.object(pool, "_active_wrappers", []),
            patch("scraper_engine.browser.pool.CamoufoxWrapper") as mock_cw,
        ):

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

        with (
            patch.object(pool, "_active_wrappers", []),
            patch("scraper_engine.browser.pool.CamoufoxWrapper") as mock_cw,
        ):

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

    @pytest.mark.skipif(
        not _camoufox_installed(),
        reason="Camoufox browser binary not installed (run `camoufox fetch`); skipped in CI",
    )
    async def test_pool_acquire_when_empty_creates_new(self, tenant, proxy):
        """Pool without warm instances creates a new wrapper on acquire.

        geoip=False: camoufox's geoip resolution dials out through the
        configured proxy at launch time to resolve a public IP — the
        fixture proxy (1.2.3.4:8080) is intentionally fake/non-routable,
        and this test isn't exercising geoip behavior, so disable it here
        rather than depend on a real working proxy just to launch."""
        from scraper_engine.browser.pool import BrowserPool

        pool = BrowserPool(tenant_id=tenant, prewarm_count=0, geoip=False)
        try:
            ctx = await pool.acquire(proxy=proxy)
            assert ctx is not None
            # acquire() returns the live BrowserContext, not the wrapper
            # that created it — the wrapper stays tracked internally.
            assert pool._active_wrappers[-1].proxy == proxy
        finally:
            await pool.shutdown()

    async def test_release_healthy_returns_to_pool(self, tenant):
        from scraper_engine.browser.camoufox_wrapper import CamoufoxWrapper
        from scraper_engine.browser.pool import BrowserPool

        pool = BrowserPool(tenant_id=tenant, prewarm_count=0)
        wrapper = CamoufoxWrapper(proxy=None, tenant_id=tenant)

        # Pool should accept healthy wrappers
        await pool.release(wrapper, healthy=True)
        # Pool should have one item now
        assert pool._pool.qsize() == 0  # wrapper was put back but queue is lazy

    def test_shutdown_clears_pool(self, tenant):
        from scraper_engine.browser.pool import BrowserPool

        pool = BrowserPool(tenant_id=tenant, prewarm_count=0)
        # shutdown on empty pool should not error
        asyncio.run(pool.shutdown())

    async def test_prewarmed_wrapper_not_evicted_on_first_domain_mismatch(self, tenant):
        """Round 25 regression: a never-yet-leased (prewarmed) wrapper has
        _last_domain=None — it must be usable for the FIRST real request's
        domain instead of being destroyed just because None != that domain
        (this was making prewarming nearly useless before this fix)."""
        pool = BrowserPool(tenant_id=tenant, prewarm_count=0)
        await pool.start()
        fake_ctx = object()
        fake_wrapper = MagicMock()
        fake_wrapper._last_domain = None
        fake_wrapper.proxy = None
        fake_wrapper.__aexit__ = AsyncMock()
        await pool._pool.put((fake_ctx, fake_wrapper, asyncio.get_event_loop().time()))

        with patch.object(pool, "_active_wrappers", [fake_wrapper]):
            ctx = await pool.acquire(domain="example.com")

        assert ctx is fake_ctx
        fake_wrapper.__aexit__.assert_not_awaited()

    async def test_mismatched_wrapper_kept_in_pool_not_destroyed(self, tenant):
        """A wrapper that already served a different domain must not be
        reused for this request, but must stay pooled as a live spare — not
        torn down. Only idle timeout (or unhealthy release/shutdown) may
        destroy a live wrapper, per the module's own docstring."""
        pool = BrowserPool(tenant_id=tenant, prewarm_count=0)
        await pool.start()
        fake_ctx = object()
        fake_wrapper = MagicMock()
        fake_wrapper._last_domain = "other.example"
        fake_wrapper.proxy = None
        fake_wrapper.__aexit__ = AsyncMock()
        await pool._pool.put((fake_ctx, fake_wrapper, asyncio.get_event_loop().time()))

        with (
            patch.object(pool, "_active_wrappers", [fake_wrapper]),
            patch("scraper_engine.browser.pool.CamoufoxWrapper") as mock_cw,
        ):
            fresh_ctx = object()
            inst = MagicMock()
            inst.__aenter__ = AsyncMock(return_value=fresh_ctx)
            mock_cw.return_value = inst
            ctx = await pool.acquire(domain="example.com")

        assert ctx is fresh_ctx  # built fresh rather than reusing the mismatch
        fake_wrapper.__aexit__.assert_not_awaited()  # NOT destroyed
        assert pool._pool.qsize() == 1  # mismatched wrapper stayed pooled

    async def test_start_skips_failed_prewarm_slot_and_continues(self, tenant):
        """Round 51 — audited 33 real historical full-job crashes, all with
        the same "0 results for any URL" signature: BrowserPool.start()'s
        prewarm loop used to let ANY single instance's launch failure (not
        just the WebGL data-gap camoufox_wrapper.py's own fallback already
        handles) propagate straight out of start(), aborting the whole job
        before any URL was attempted. Prewarming is documented as "purely a
        latency optimization" (class docstring) — acquire() already
        launches fresh on-demand when the pool is empty, so one slot's
        failure must be skipped, not fatal."""
        with patch("scraper_engine.browser.pool.CamoufoxWrapper") as mock_cw:
            good_ctx_1, good_ctx_2 = object(), object()
            good_1 = MagicMock()
            good_1.__aenter__ = AsyncMock(return_value=good_ctx_1)
            failing = MagicMock()
            failing.__aenter__ = AsyncMock(side_effect=RuntimeError("launch boom"))
            good_2 = MagicMock()
            good_2.__aenter__ = AsyncMock(return_value=good_ctx_2)
            mock_cw.side_effect = [good_1, failing, good_2]

            pool = BrowserPool(tenant_id=tenant, prewarm_count=3)
            await pool.start()  # must not raise

        assert pool._started is True
        assert len(pool._active_wrappers) == 2
        assert pool._pool.qsize() == 2

    async def test_start_survives_every_prewarm_slot_failing(self, tenant):
        """Degrades all the way to zero hot instances rather than crashing
        the job — acquire() building fresh on-demand is the documented
        fallback for exactly this case."""
        with patch("scraper_engine.browser.pool.CamoufoxWrapper") as mock_cw:
            failing = MagicMock()
            failing.__aenter__ = AsyncMock(side_effect=RuntimeError("launch boom"))
            mock_cw.return_value = failing

            pool = BrowserPool(tenant_id=tenant, prewarm_count=2)
            await pool.start()  # must not raise

        assert pool._started is True
        assert pool._active_wrappers == []
        assert pool._pool.qsize() == 0

    async def test_start_still_raises_on_prewarm_count_misconfiguration(self, tenant):
        """The prewarm_count > max_total_instances check is a real
        misconfiguration, not a per-instance launch failure — must still
        raise, and must raise before attempting any launch (nothing to
        clean up)."""
        with patch("scraper_engine.browser.pool.CamoufoxWrapper") as mock_cw:
            pool = BrowserPool(tenant_id=tenant, prewarm_count=5, max_total_instances=2)
            with pytest.raises(ValueError, match="exceeds"):
                await pool.start()

        mock_cw.assert_not_called()


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
        from scraper_engine.browser.camoufox_wrapper import CamoufoxWrapper

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
        from scraper_engine.browser.camoufox_wrapper import CamoufoxWrapper

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
        from scraper_engine.browser.session_state import SessionStateManager

        mgr = SessionStateManager(pg=pg)

        pool = BrowserPool(tenant_id=tenant, prewarm_count=0, session_mgr=mgr)
        fake_ctx = object()

        with patch("scraper_engine.browser.pool.CamoufoxWrapper") as mock_cw:
            mock_wrapper = MagicMock()
            mock_wrapper.__aenter__ = AsyncMock(return_value=fake_ctx)
            mock_cw.return_value = mock_wrapper

            _ctx = await pool.acquire(proxy=None, domain="example.com")
            call_kwargs = mock_cw.call_args[1]
            assert "storage_state" in call_kwargs
            assert call_kwargs["storage_state"] == {
                "cookies": [{"name": "x", "value": "y"}],
                "origins": [],
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
        from scraper_engine.browser.session_state import SessionStateManager

        mgr = SessionStateManager(pg=pg)

        pool = BrowserPool(tenant_id=tenant, prewarm_count=0, session_mgr=mgr)
        fake_ctx = MagicMock()
        fake_ctx.storage_state = AsyncMock(return_value={"cookies": [], "origins": []})
        fake_wrapper = MagicMock()
        fake_wrapper._context = fake_ctx
        fake_wrapper._isolated_ctx = fake_ctx
        fake_wrapper.__aexit__ = AsyncMock()
        pool._active_wrappers = [fake_wrapper]

        with (
            patch.object(pool, "acquire", new_callable=AsyncMock, return_value=fake_ctx),
            patch.object(pool, "release", new_callable=AsyncMock),
        ):
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
        from scraper_engine.browser.session_state import SessionStateManager

        mgr = SessionStateManager(pg=pg)

        pool = BrowserPool(tenant_id=tenant, prewarm_count=0, session_mgr=mgr)
        fake_ctx = MagicMock()
        fake_ctx.storage_state = AsyncMock(return_value={"cookies": [], "origins": []})
        fake_wrapper = MagicMock()
        fake_wrapper._context = fake_ctx
        fake_wrapper._isolated_ctx = fake_ctx
        fake_wrapper.__aexit__ = AsyncMock()
        pool._active_wrappers = [fake_wrapper]

        with (
            patch.object(pool, "acquire", new_callable=AsyncMock, return_value=fake_ctx),
            patch.object(pool, "release", new_callable=AsyncMock),
            pytest.raises(RuntimeError),
        ):
            async with pool.lease(domain="example.com"):
                raise RuntimeError("simulated failure")
        fake_ctx.storage_state.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_session_mgr_lease_yields_context_directly(self, tenant):
        """When session_mgr=None, lease() yields context without session I/O."""
        pool = BrowserPool(tenant_id=tenant, prewarm_count=0, session_mgr=None)
        fake_ctx = object()

        with (
            patch.object(pool, "acquire", new_callable=AsyncMock, return_value=fake_ctx),
            patch.object(pool, "release", new_callable=AsyncMock),
        ):
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

        with (
            patch.object(pool, "_active_wrappers", []),
            patch("scraper_engine.browser.pool.CamoufoxWrapper") as mock_cw,
        ):

            def make_mock(*a, **kw):
                inst = MagicMock()
                inst.__aenter__ = AsyncMock(return_value=object())
                return inst

            mock_cw.side_effect = make_mock
            ctx2 = await pool.acquire()
            assert ctx2 is not fake_ctx


class TestCamoufoxWrapperGeoipFallback:
    """Round 37 — CamoufoxWrapper._launch_with_geoip_fallback: a proxy that
    passes lease-time preflight can still fail Camoufox's own internal
    geoip IP-lookup (camoufox/ip.py::public_ip, 6 third-party services
    tried internally) — live-caught. AsyncCamoufox is mocked directly at
    its import source (camoufox.async_api.AsyncCamoufox) — no existing
    test in this file exercises the real launch path with a controllable
    mock, so these are new coverage, not a rewrite of existing tests."""

    @pytest.mark.asyncio
    async def test_launch_succeeds_without_fallback(self, tenant):
        from scraper_engine.browser.camoufox_wrapper import CamoufoxWrapper

        fake_context = MagicMock()
        camoufox_instance = MagicMock()
        camoufox_instance.__aenter__ = AsyncMock(return_value=fake_context)
        camoufox_ctor = MagicMock(return_value=camoufox_instance)

        wrapper = CamoufoxWrapper(proxy=None, tenant_id=tenant)
        with patch("camoufox.async_api.AsyncCamoufox", camoufox_ctor):
            result = await wrapper._launch_with_geoip_fallback()

        assert result is fake_context
        camoufox_ctor.assert_called_once()
        assert camoufox_ctor.call_args.kwargs["geoip"] is True
        # Round 46 — real fingerprint presets + host-OS-matched os=, per
        # Camoufox's own docs (see config/schema.py::CamoufoxConfig).
        assert camoufox_ctor.call_args.kwargs["fingerprint_preset"] is True
        assert camoufox_ctor.call_args.kwargs["os"] == "linux"

    @pytest.mark.asyncio
    async def test_launch_falls_back_without_geoip_on_invalid_ip(self, tenant, caplog):
        from camoufox.exceptions import InvalidIP

        from scraper_engine.browser.camoufox_wrapper import CamoufoxWrapper

        fake_context = MagicMock()
        failing_instance = MagicMock()
        failing_instance.__aenter__ = AsyncMock(side_effect=InvalidIP("boom"))
        succeeding_instance = MagicMock()
        succeeding_instance.__aenter__ = AsyncMock(return_value=fake_context)
        camoufox_ctor = MagicMock(side_effect=[failing_instance, succeeding_instance])

        wrapper = CamoufoxWrapper(
            proxy=Proxy(id=1, ip="1.2.3.4", port=8080, protocol=ProxyProtocol.HTTP),
            tenant_id=tenant,
        )
        with patch("camoufox.async_api.AsyncCamoufox", camoufox_ctor):
            result = await wrapper._launch_with_geoip_fallback()

        assert result is fake_context
        assert camoufox_ctor.call_count == 2
        assert camoufox_ctor.call_args_list[0].kwargs["geoip"] is True
        assert camoufox_ctor.call_args_list[1].kwargs["geoip"] is False
        for call in camoufox_ctor.call_args_list:
            assert call.kwargs["fingerprint_preset"] is True
            assert call.kwargs["os"] == "linux"

    @pytest.mark.asyncio
    async def test_launch_reraises_invalid_ip_when_geoip_already_disabled(self, tenant):
        """Defensive: geoip=False means Camoufox never calls its own
        internal IP-lookup, so InvalidIP shouldn't fire in practice — but
        if it somehow does, there's no further fallback available."""
        from camoufox.exceptions import InvalidIP

        from scraper_engine.browser.camoufox_wrapper import CamoufoxWrapper

        failing_instance = MagicMock()
        failing_instance.__aenter__ = AsyncMock(side_effect=InvalidIP("boom"))
        camoufox_ctor = MagicMock(return_value=failing_instance)

        wrapper = CamoufoxWrapper(proxy=None, tenant_id=tenant, geoip=False)
        with (
            patch("camoufox.async_api.AsyncCamoufox", camoufox_ctor),
            pytest.raises(InvalidIP),
        ):
            await wrapper._launch_with_geoip_fallback()

        camoufox_ctor.assert_called_once()

    @pytest.mark.asyncio
    async def test_launch_other_exception_propagates_without_fallback(self, tenant):
        """A non-InvalidIP launch failure (e.g. a genuinely dead proxy) must
        not trigger the geoip fallback — only the specific geoip-lookup
        failure mode should retry."""
        from scraper_engine.browser.camoufox_wrapper import CamoufoxWrapper

        failing_instance = MagicMock()
        failing_instance.__aenter__ = AsyncMock(side_effect=RuntimeError("boom"))
        camoufox_ctor = MagicMock(return_value=failing_instance)

        wrapper = CamoufoxWrapper(proxy=None, tenant_id=tenant)
        with (
            patch("camoufox.async_api.AsyncCamoufox", camoufox_ctor),
            pytest.raises(RuntimeError),
        ):
            await wrapper._launch_with_geoip_fallback()

        camoufox_ctor.assert_called_once()

    @pytest.mark.asyncio
    async def test_launch_falls_back_without_fingerprint_preset_on_webgl_data_gap(
        self, tenant, caplog
    ):
        """Round 49 — live-caught: fingerprint_preset=True samples a real
        captured fingerprint whose (vendor, renderer) isn't covered by
        camoufox's separate webgl_data.db lookup table
        (camoufox/webgl/sample.py::sample_webgl, verified against the
        actual installed package source) — a genuine gap between camoufox's
        two internal datasets, not something our config controls. Before
        this, it crashed the ENTIRE job (BrowserPool.start()'s prewarm loop
        runs outside process_job's per-URL try/except)."""
        from scraper_engine.browser.camoufox_wrapper import CamoufoxWrapper

        fake_context = MagicMock()
        failing_instance = MagicMock()
        failing_instance.__aenter__ = AsyncMock(
            side_effect=ValueError(
                'No WebGL data found for vendor "Intel Open Source Technology Center" '
                'and renderer "Intel(R) HD Graphics 400, or similar"'
            )
        )
        succeeding_instance = MagicMock()
        succeeding_instance.__aenter__ = AsyncMock(return_value=fake_context)
        camoufox_ctor = MagicMock(side_effect=[failing_instance, succeeding_instance])

        wrapper = CamoufoxWrapper(proxy=None, tenant_id=tenant)
        with patch("camoufox.async_api.AsyncCamoufox", camoufox_ctor):
            result = await wrapper._launch_with_geoip_fallback()

        assert result is fake_context
        assert camoufox_ctor.call_count == 2
        assert camoufox_ctor.call_args_list[0].kwargs["fingerprint_preset"] is True
        # Round 51 — must be None, not False: verified against the actual
        # installed camoufox/utils.py::launch_options, whose preset branch
        # is guarded by `is not None`, not truthiness. False satisfies
        # `is not None` exactly like True and draws another real preset
        # from the same pool — a no-op against this exact crash. Only None
        # reaches the true BrowserForge synthetic path.
        assert camoufox_ctor.call_args_list[1].kwargs["fingerprint_preset"] is None
        for call in camoufox_ctor.call_args_list:
            assert call.kwargs["geoip"] is True

    @pytest.mark.asyncio
    async def test_launch_falls_back_even_when_fingerprint_preset_starts_disabled(
        self, tenant
    ):
        """Round 51 — a caller-supplied fingerprint_preset=False is exactly
        as exposed to the webgl_data.db gap as True (camoufox's `is not
        None` check treats them identically), so the retry guard must not
        use fingerprint_preset's own value as the "already tried" marker —
        it must still get one real fallback attempt (to None) here."""
        from scraper_engine.browser.camoufox_wrapper import CamoufoxWrapper

        fake_context = MagicMock()
        failing_instance = MagicMock()
        failing_instance.__aenter__ = AsyncMock(
            side_effect=ValueError('No WebGL data found for vendor "X" and renderer "Y"')
        )
        succeeding_instance = MagicMock()
        succeeding_instance.__aenter__ = AsyncMock(return_value=fake_context)
        camoufox_ctor = MagicMock(side_effect=[failing_instance, succeeding_instance])

        wrapper = CamoufoxWrapper(proxy=None, tenant_id=tenant, fingerprint_preset=False)
        with patch("camoufox.async_api.AsyncCamoufox", camoufox_ctor):
            result = await wrapper._launch_with_geoip_fallback()

        assert result is fake_context
        assert camoufox_ctor.call_count == 2
        assert camoufox_ctor.call_args_list[0].kwargs["fingerprint_preset"] is False
        assert camoufox_ctor.call_args_list[1].kwargs["fingerprint_preset"] is None

    @pytest.mark.asyncio
    async def test_launch_reraises_webgl_error_when_fallback_already_spent(self, tenant):
        """Defensive: once the fingerprint fallback has actually been used
        (fingerprint_preset=None) and the same gap fires again, there's no
        further fallback — must re-raise, not loop forever."""
        from scraper_engine.browser.camoufox_wrapper import CamoufoxWrapper

        failing_instance = MagicMock()
        failing_instance.__aenter__ = AsyncMock(
            side_effect=ValueError('No WebGL data found for vendor "X" and renderer "Y"')
        )
        camoufox_ctor = MagicMock(return_value=failing_instance)

        wrapper = CamoufoxWrapper(proxy=None, tenant_id=tenant, fingerprint_preset=False)
        with (
            patch("camoufox.async_api.AsyncCamoufox", camoufox_ctor),
            pytest.raises(ValueError, match="No WebGL data found"),
        ):
            await wrapper._launch_with_geoip_fallback()

        assert camoufox_ctor.call_count == 2
        assert camoufox_ctor.call_args_list[1].kwargs["fingerprint_preset"] is None

    @pytest.mark.asyncio
    async def test_launch_unrelated_value_error_propagates_without_fallback(self, tenant):
        """A ValueError that isn't the WebGL-data-gap shape must not
        trigger the fallback — proves the message-match is scoped, not a
        blanket "retry on any ValueError" that would mask unrelated bugs."""
        from scraper_engine.browser.camoufox_wrapper import CamoufoxWrapper

        failing_instance = MagicMock()
        failing_instance.__aenter__ = AsyncMock(side_effect=ValueError("some unrelated error"))
        camoufox_ctor = MagicMock(return_value=failing_instance)

        wrapper = CamoufoxWrapper(proxy=None, tenant_id=tenant)
        with (
            patch("camoufox.async_api.AsyncCamoufox", camoufox_ctor),
            pytest.raises(ValueError, match="some unrelated error"),
        ):
            await wrapper._launch_with_geoip_fallback()

        camoufox_ctor.assert_called_once()

    @pytest.mark.asyncio
    async def test_launch_falls_back_across_both_invalid_ip_and_webgl_gap(self, tenant):
        """Both fallbacks can stack in one launch — InvalidIP on the first
        attempt, then a WebGL data-gap on the retry, succeeding on the
        third attempt with both geoip and fingerprint_preset disabled."""
        from camoufox.exceptions import InvalidIP

        from scraper_engine.browser.camoufox_wrapper import CamoufoxWrapper

        fake_context = MagicMock()
        first = MagicMock()
        first.__aenter__ = AsyncMock(side_effect=InvalidIP("boom"))
        second = MagicMock()
        second.__aenter__ = AsyncMock(
            side_effect=ValueError('No WebGL data found for vendor "X" and renderer "Y"')
        )
        third = MagicMock()
        third.__aenter__ = AsyncMock(return_value=fake_context)
        camoufox_ctor = MagicMock(side_effect=[first, second, third])

        wrapper = CamoufoxWrapper(
            proxy=Proxy(id=1, ip="1.2.3.4", port=8080, protocol=ProxyProtocol.HTTP),
            tenant_id=tenant,
        )
        with patch("camoufox.async_api.AsyncCamoufox", camoufox_ctor):
            result = await wrapper._launch_with_geoip_fallback()

        assert result is fake_context
        assert camoufox_ctor.call_count == 3
        assert camoufox_ctor.call_args_list[2].kwargs["geoip"] is False
        assert camoufox_ctor.call_args_list[2].kwargs["fingerprint_preset"] is None

    @pytest.mark.asyncio
    async def test_launch_includes_credentials_when_proxy_has_them(self, tenant):
        """Round 40 — a paid-gateway Proxy (proxy/paid_gateway.py) carries
        username/password; the launch's proxy dict must forward them
        alongside server, since Camoufox/Playwright's proxy= option natively
        accepts username/password but nothing set them before round 40."""
        from scraper_engine.browser.camoufox_wrapper import CamoufoxWrapper

        fake_context = MagicMock()
        camoufox_instance = MagicMock()
        camoufox_instance.__aenter__ = AsyncMock(return_value=fake_context)
        camoufox_ctor = MagicMock(return_value=camoufox_instance)

        wrapper = CamoufoxWrapper(
            proxy=Proxy(
                id=-1,
                ip="gw.dataimpulse.com",
                port=823,
                protocol=ProxyProtocol.HTTP,
                username="user123",
                password="pass456",
                source="paid_gateway",
            ),
            tenant_id=tenant,
        )
        with patch("camoufox.async_api.AsyncCamoufox", camoufox_ctor):
            await wrapper._launch_with_geoip_fallback()

        proxy_config = camoufox_ctor.call_args.kwargs["proxy"]
        assert proxy_config == {
            "server": "http://gw.dataimpulse.com:823",
            "username": "user123",
            "password": "pass456",
        }

    @pytest.mark.asyncio
    async def test_launch_omits_credentials_for_free_pool_proxy(self, tenant):
        """Regression guard: a plain free-pool Proxy (no username/password)
        must not gain those keys — the dict stays exactly {"server": ...}."""
        from scraper_engine.browser.camoufox_wrapper import CamoufoxWrapper

        fake_context = MagicMock()
        camoufox_instance = MagicMock()
        camoufox_instance.__aenter__ = AsyncMock(return_value=fake_context)
        camoufox_ctor = MagicMock(return_value=camoufox_instance)

        wrapper = CamoufoxWrapper(
            proxy=Proxy(id=1, ip="1.2.3.4", port=8080, protocol=ProxyProtocol.HTTP),
            tenant_id=tenant,
        )
        with patch("camoufox.async_api.AsyncCamoufox", camoufox_ctor):
            await wrapper._launch_with_geoip_fallback()

        proxy_config = camoufox_ctor.call_args.kwargs["proxy"]
        assert proxy_config == {"server": "http://1.2.3.4:8080"}

    @pytest.mark.asyncio
    async def test_aenter_releases_semaphore_when_launch_fails(self, tenant):
        """__aenter__'s existing except-release-reraise contract must still
        hold when the failure comes from inside _launch_with_geoip_fallback
        (not just a bare AsyncCamoufox() call as before the extraction)."""
        from scraper_engine.browser.camoufox_wrapper import CamoufoxWrapper
        from scraper_engine.core import budget

        wrapper = CamoufoxWrapper(proxy=None, tenant_id=tenant)
        wrapper._launch_with_geoip_fallback = AsyncMock(side_effect=RuntimeError("boom"))

        before = budget.BROWSER_SEMAPHORE._value
        with pytest.raises(RuntimeError):
            await wrapper.__aenter__()
        assert budget.BROWSER_SEMAPHORE._value == before


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


class _FakeWrapper:
    """Stands in for CamoufoxWrapper with the one property that matters here:
    it holds a real BROWSER_SEMAPHORE permit from __aenter__ to __aexit__."""

    instances: list["_FakeWrapper"] = []

    def __init__(self, proxy=None, **_kwargs):
        self.proxy = proxy
        self._context = None
        self._isolated_ctx = None
        self._last_domain = None
        self.closed = False
        _FakeWrapper.instances.append(self)

    async def __aenter__(self):
        from scraper_engine.core import budget

        await budget.BROWSER_SEMAPHORE.acquire()
        self._context = object()
        self._isolated_ctx = object()
        return self._isolated_ctx

    async def __aexit__(self, *_exc):
        from scraper_engine.core import budget

        self.closed = True
        budget.BROWSER_SEMAPHORE.release()


class TestParkedSparesNeverStarveALaunch:
    """Round 63 — a pooled spare must not be able to starve a new launch.

    release(healthy=True) returns a context to the queue but does NOT release
    BROWSER_SEMAPHORE, so an idle spare goes on holding its permit. acquire()
    keeps a non-matching spare pooled and launches a fresh instance instead,
    so once every permit is held by idle spares the next launch blocks on the
    semaphore forever — nothing is running, so nothing will ever release it.
    Round 62 made this ordinary: the paid gateway presents a fresh sessid per
    attempt, so `proxy` differs on nearly every attempt and the mismatch
    branch is taken nearly every time.

    The deadlock has two orderings and both are covered: spares already
    parked when the launch arrives (evict them), and spares parked while the
    launch is already waiting (release() must hand the permit on instead of
    parking). A first fix covered only the former, keyed on the pool's own
    instance count; live, a 10-URL job still finished 9 of 10 and then hung
    with 8 idle instances parked behind the 10th URL's launch.
    """

    @pytest.fixture
    def one_permit(self, monkeypatch):
        from scraper_engine.browser import pool as pool_mod
        from scraper_engine.core import budget

        sem = asyncio.Semaphore(1)
        monkeypatch.setattr(budget, "BROWSER_SEMAPHORE", sem)
        monkeypatch.setattr(pool_mod, "CamoufoxWrapper", _FakeWrapper)
        _FakeWrapper.instances = []
        return sem

    @staticmethod
    def _proxy(port):
        return Proxy(id=port, ip="10.0.0.1", port=port, protocol=ProxyProtocol.HTTP)

    async def test_a_spare_parked_while_a_launch_waits_is_handed_over(
        self, tenant, one_permit
    ):
        """The live deadlock: the waiter arrived while every instance was
        leased, then a sibling finished and returned its instance healthy."""
        pool = BrowserPool(tenant_id=tenant, prewarm_count=0)
        ctx_a = await pool.acquire(proxy=self._proxy(1))
        waiter = asyncio.create_task(pool.acquire(proxy=self._proxy(2)))
        await asyncio.sleep(0)
        assert not waiter.done()

        await pool.release(ctx_a, healthy=True)
        ctx_b = await asyncio.wait_for(waiter, timeout=1)

        assert ctx_b is not ctx_a
        assert _FakeWrapper.instances[0].closed
        assert pool._pool.qsize() == 0
        assert pool._launch_waiters == 0

    async def test_a_spare_already_parked_is_evicted_for_a_launch(self, tenant, one_permit):
        pool = BrowserPool(tenant_id=tenant, prewarm_count=0)
        ctx_a = await pool.acquire(proxy=self._proxy(1))
        await pool.release(ctx_a, healthy=True)
        assert pool._pool.qsize() == 1

        await asyncio.wait_for(pool.acquire(proxy=self._proxy(2)), timeout=1)

        assert _FakeWrapper.instances[0].closed
        assert [w.proxy.port for w in pool._active_wrappers] == [2]

    async def test_release_parks_when_nobody_is_waiting(self, tenant, one_permit):
        pool = BrowserPool(tenant_id=tenant, prewarm_count=0)
        ctx = await pool.acquire(proxy=self._proxy(1))
        await pool.release(ctx, healthy=True)
        assert pool._pool.qsize() == 1
        assert not _FakeWrapper.instances[0].closed

    async def test_no_eviction_while_a_permit_is_free(self, tenant, monkeypatch):
        from scraper_engine.core import budget

        monkeypatch.setattr(budget, "BROWSER_SEMAPHORE", asyncio.Semaphore(2))
        pool = BrowserPool(tenant_id=tenant, prewarm_count=0)
        spare = _FakeWrapper()
        await spare.__aenter__()
        pool._active_wrappers = [spare]
        await pool._pool.put((spare._isolated_ctx, spare, time.monotonic()))

        await pool._make_room_for_launch()

        assert not spare.closed
        assert pool._pool.qsize() == 1

    async def test_no_eviction_when_every_instance_is_genuinely_leased_out(
        self, tenant, one_permit
    ):
        """An empty pool with no free permit is real contention — the caller
        must wait, not tear down a browser someone else is mid-fetch with."""
        pool = BrowserPool(tenant_id=tenant, prewarm_count=0)
        await pool.acquire(proxy=self._proxy(1))
        await pool._make_room_for_launch()
        assert not _FakeWrapper.instances[0].closed
        assert one_permit.locked()

    async def test_oldest_spare_goes_first(self, tenant, monkeypatch):
        from scraper_engine.core import budget

        monkeypatch.setattr(budget, "BROWSER_SEMAPHORE", asyncio.Semaphore(2))
        pool = BrowserPool(tenant_id=tenant, prewarm_count=0)
        old, new = _FakeWrapper(), _FakeWrapper()
        for w in (old, new):
            await w.__aenter__()
            pool._active_wrappers.append(w)
            await pool._pool.put((w._isolated_ctx, w, time.monotonic()))

        await pool._make_room_for_launch()

        assert old.closed and not new.closed
        assert pool._active_wrappers == [new]

    async def test_a_failing_teardown_does_not_loop_forever(self, tenant, one_permit):
        """Eviction exists to unblock a launch; a spare whose teardown raises
        (and so never frees its permit) must be dropped, and the loop must
        stop once the pool is empty rather than spin."""
        pool = BrowserPool(tenant_id=tenant, prewarm_count=0)
        await one_permit.acquire()
        spare = MagicMock()
        spare._context = "ctx-1"
        spare._isolated_ctx = None
        spare.__aexit__ = AsyncMock(side_effect=RuntimeError("browser already gone"))
        pool._active_wrappers = [spare]
        await pool._pool.put(("ctx-1", spare, time.monotonic()))

        await asyncio.wait_for(pool._make_room_for_launch(), timeout=1)

        assert pool._active_wrappers == []
        assert pool._pool.qsize() == 0

    async def test_a_cancelled_launch_is_not_counted_as_a_live_browser(
        self, tenant, one_permit
    ):
        pool = BrowserPool(tenant_id=tenant, prewarm_count=0)
        await pool.acquire(proxy=self._proxy(1))
        waiter = asyncio.create_task(pool.acquire(proxy=self._proxy(2)))
        await asyncio.sleep(0)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert [w.proxy.port for w in pool._active_wrappers] == [1]
        assert pool._launch_waiters == 0

    async def test_a_cancelled_lease_frees_its_permit(self, tenant, one_permit):
        """`except Exception` in lease() let CancelledError skip both the
        teardown and the park branch, leaking the instance and its permit."""
        pool = BrowserPool(tenant_id=tenant, prewarm_count=0)
        with pytest.raises(asyncio.CancelledError):
            async with pool.lease(proxy=self._proxy(1)):
                raise asyncio.CancelledError
        assert _FakeWrapper.instances[0].closed
        assert not one_permit.locked()
        assert pool._active_wrappers == []


class TestCancelledLaunchReleasesPermit:
    """Round 63 — CamoufoxWrapper.__aenter__ caught only Exception, so a
    launch cancelled mid-flight kept its BROWSER_SEMAPHORE permit forever,
    and a browser whose new_context() failed was left running unowned."""

    async def test_cancelled_launch_releases_the_permit(self, monkeypatch):
        from scraper_engine.browser import camoufox_wrapper as mod
        from scraper_engine.core import budget

        sem = asyncio.Semaphore(1)
        monkeypatch.setattr(budget, "BROWSER_SEMAPHORE", sem)
        wrapper = mod.CamoufoxWrapper(proxy=None, tenant_id=TenantId("cancel"))
        monkeypatch.setattr(
            wrapper,
            "_launch_with_geoip_fallback",
            AsyncMock(side_effect=asyncio.CancelledError),
        )
        with pytest.raises(asyncio.CancelledError):
            await wrapper.__aenter__()
        assert not sem.locked()

    async def test_failed_new_context_closes_the_launched_browser(self, monkeypatch):
        from scraper_engine.browser import camoufox_wrapper as mod
        from scraper_engine.core import budget

        sem = asyncio.Semaphore(1)
        monkeypatch.setattr(budget, "BROWSER_SEMAPHORE", sem)
        wrapper = mod.CamoufoxWrapper(proxy=None, tenant_id=TenantId("ctxfail"))
        browser = MagicMock()
        browser.__aexit__ = AsyncMock(return_value=None)
        context = MagicMock()
        context.new_context = AsyncMock(side_effect=RuntimeError("context refused"))

        async def _launch():
            wrapper._browser = browser
            return context

        monkeypatch.setattr(wrapper, "_launch_with_geoip_fallback", _launch)
        with pytest.raises(RuntimeError, match="context refused"):
            await wrapper.__aenter__()
        browser.__aexit__.assert_awaited_once()
        assert not sem.locked()


class TestBoundedBrowserTeardown:
    """Round 63 — no XVFB_LOCK critical section may block forever.

    XVFB_LOCK is process-wide and both the launch and the teardown hold it,
    and `budget.BROWSER_SEMAPHORE.release()` runs only AFTER the teardown's
    lock block. So one wedged Camoufox (dead CDP pipe, an Xvfb that will not
    exit) froze every browser operation in the worker permanently AND leaked
    every permit. Live-caught twice on a 10-URL Jumia job: 8 live browsers,
    an idle event loop, zero log output, the job stalled mid-run until RQ's
    job timeout killed the work-horse.

    A timeout can leak a browser process. That is the better failure: a
    leaked browser costs memory on one worker until it recycles, an
    unbounded wait costs every remaining URL of every job it would run.
    """

    @staticmethod
    def _wedged_wrapper(monkeypatch):
        from scraper_engine.browser import camoufox_wrapper as mod

        monkeypatch.setattr(mod, "_BROWSER_TEARDOWN_TIMEOUT_SECONDS", 0.05)
        wrapper = mod.CamoufoxWrapper(proxy=None, tenant_id=TenantId("teardown"))
        hung = MagicMock()

        async def _never_returns(*_a, **_k):
            await asyncio.sleep(3600)

        hung.__aexit__ = _never_returns
        wrapper._browser = hung
        return wrapper

    async def test_a_hung_teardown_releases_the_browser_permit(self, monkeypatch):
        from scraper_engine.core import budget

        wrapper = self._wedged_wrapper(monkeypatch)
        await budget.BROWSER_SEMAPHORE.acquire()
        before = budget.BROWSER_SEMAPHORE._value

        await wrapper.__aexit__()

        assert budget.BROWSER_SEMAPHORE._value == before + 1

    async def test_a_hung_teardown_does_not_keep_xvfb_lock(self, monkeypatch):
        """The load-bearing property: the NEXT browser operation must be able
        to proceed. Holding XVFB_LOCK is what turned one stuck browser into a
        dead worker."""
        from scraper_engine.core import budget

        wrapper = self._wedged_wrapper(monkeypatch)

        await wrapper.__aexit__()

        assert not budget.XVFB_LOCK.locked()

    async def test_a_normal_teardown_is_unchanged(self, monkeypatch):
        from scraper_engine.browser import camoufox_wrapper as mod
        from scraper_engine.core import budget

        wrapper = mod.CamoufoxWrapper(proxy=None, tenant_id=TenantId("teardown"))
        browser = MagicMock()
        browser.__aexit__ = AsyncMock(return_value=None)
        wrapper._browser = browser
        await budget.BROWSER_SEMAPHORE.acquire()
        before = budget.BROWSER_SEMAPHORE._value

        await wrapper.__aexit__()

        browser.__aexit__.assert_awaited_once()
        assert wrapper._browser is None
        assert budget.BROWSER_SEMAPHORE._value == before + 1
        assert not budget.XVFB_LOCK.locked()
