# tests/unit/test_dlq_reaper.py
"""dlq_reaper tests — transient-failure auto-retry eligibility + re-enqueue
(round 34)."""

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from scraper_engine.config.schema import DlqReaperConfig, ProxyTierConfig
from scraper_engine.core.models import FailureCategory
from scraper_engine.core.tenant import TenantId
from scraper_engine.orchestrator.circuit_breaker import CircuitState
from scraper_engine.proxy import dlq_reaper
from scraper_engine.proxy.pool_health import PoolHealthState
from scraper_engine.storage.dlq import DeadLetterEntry


def make_entry(
    category=FailureCategory.PROXY_EXHAUSTED,
    level_attempted=2,
    url="http://example.com/dead",
    job_id="job-1",
    auto_retry_count=0,
    dead_at=None,
    retry_not_before=None,
):
    now = datetime.now(UTC)
    return DeadLetterEntry(
        retry_not_before=retry_not_before,
        id=1,
        job_id=job_id,
        tenant_id="test",
        url=url,
        failure_category=category,
        error_message="dead",
        level_attempted=level_attempted,
        auto_retry_count=auto_retry_count,
        enqueued_at=now,
        dead_at=dead_at or now,
    )


@pytest.fixture
def tenant():
    return TenantId("test")


class TestIsEligible:
    @pytest.mark.asyncio
    async def test_proxy_exhausted_eligible_when_tier_healthy(self, monkeypatch):
        redis = AsyncMock()
        cb = AsyncMock()
        tier_config = ProxyTierConfig()
        monkeypatch.setattr(
            "scraper_engine.proxy.dlq_reaper.pool_current_state",
            AsyncMock(return_value=PoolHealthState.HEALTHY),
        )
        entry = make_entry(category=FailureCategory.PROXY_EXHAUSTED)

        assert await dlq_reaper._is_eligible(entry, redis, cb, tier_config) is True

    @pytest.mark.asyncio
    async def test_proxy_exhausted_not_eligible_when_tier_still_degraded(self, monkeypatch):
        redis = AsyncMock()
        cb = AsyncMock()
        tier_config = ProxyTierConfig()
        monkeypatch.setattr(
            "scraper_engine.proxy.dlq_reaper.pool_current_state",
            AsyncMock(return_value=PoolHealthState.DEGRADED),
        )
        entry = make_entry(category=FailureCategory.PROXY_EXHAUSTED)

        assert await dlq_reaper._is_eligible(entry, redis, cb, tier_config) is False

    @pytest.mark.asyncio
    async def test_level3_checks_tier2_when_fallback_enabled(self, monkeypatch):
        """Round 37 — allow_tier2_fallback_for_tier3 changes what a level-3
        lease actually depends on; the eligibility check must follow that,
        not check tier 3's raw (structurally-always-CRITICAL-on-free-only-
        sources) pool_health count."""
        redis = AsyncMock()
        cb = AsyncMock()
        tier_config = ProxyTierConfig(allow_tier2_fallback_for_tier3=True)
        checked_tiers = []

        async def fake_pool_current_state(redis_arg, tier):
            checked_tiers.append(tier)
            return PoolHealthState.HEALTHY

        monkeypatch.setattr(
            "scraper_engine.proxy.dlq_reaper.pool_current_state", fake_pool_current_state
        )
        entry = make_entry(category=FailureCategory.PROXY_EXHAUSTED, level_attempted=3)

        assert await dlq_reaper._is_eligible(entry, redis, cb, tier_config) is True
        assert checked_tiers == [2]

    @pytest.mark.asyncio
    async def test_level3_checks_tier3_when_fallback_disabled(self, monkeypatch):
        """Default config (fallback off) — unchanged behavior, tier 3's own
        health still gates a level-3 exhaustion's retry eligibility."""
        redis = AsyncMock()
        cb = AsyncMock()
        tier_config = ProxyTierConfig(allow_tier2_fallback_for_tier3=False)
        checked_tiers = []

        async def fake_pool_current_state(redis_arg, tier):
            checked_tiers.append(tier)
            return PoolHealthState.HEALTHY

        monkeypatch.setattr(
            "scraper_engine.proxy.dlq_reaper.pool_current_state", fake_pool_current_state
        )
        entry = make_entry(category=FailureCategory.PROXY_EXHAUSTED, level_attempted=3)

        assert await dlq_reaper._is_eligible(entry, redis, cb, tier_config) is True
        assert checked_tiers == [3]

    @pytest.mark.asyncio
    async def test_level2_exhaustion_unaffected_by_fallback_flag(self, monkeypatch):
        """The fallback only applies to level-3 leases (round 33) — a
        level-2 exhaustion must always check tier 2, regardless of the
        flag's value."""
        redis = AsyncMock()
        cb = AsyncMock()
        tier_config = ProxyTierConfig(allow_tier2_fallback_for_tier3=True)
        checked_tiers = []

        async def fake_pool_current_state(redis_arg, tier):
            checked_tiers.append(tier)
            return PoolHealthState.HEALTHY

        monkeypatch.setattr(
            "scraper_engine.proxy.dlq_reaper.pool_current_state", fake_pool_current_state
        )
        entry = make_entry(category=FailureCategory.PROXY_EXHAUSTED, level_attempted=2)

        assert await dlq_reaper._is_eligible(entry, redis, cb, tier_config) is True
        assert checked_tiers == [2]

    @pytest.mark.asyncio
    async def test_level2_checks_tier1_when_fallback_enabled(self, monkeypatch):
        """Round 39 — same substitution as round 37's level-3/tier-2 case,
        one tier down: allow_tier1_fallback_for_tier2 changes what a
        level-2 lease actually depends on."""
        redis = AsyncMock()
        cb = AsyncMock()
        tier_config = ProxyTierConfig(allow_tier1_fallback_for_tier2=True)
        checked_tiers = []

        async def fake_pool_current_state(redis_arg, tier):
            checked_tiers.append(tier)
            return PoolHealthState.HEALTHY

        monkeypatch.setattr(
            "scraper_engine.proxy.dlq_reaper.pool_current_state", fake_pool_current_state
        )
        entry = make_entry(category=FailureCategory.PROXY_EXHAUSTED, level_attempted=2)

        assert await dlq_reaper._is_eligible(entry, redis, cb, tier_config) is True
        assert checked_tiers == [1]

    @pytest.mark.asyncio
    async def test_level2_checks_tier2_when_fallback_disabled(self, monkeypatch):
        """Default config (fallback off) — unchanged behavior, tier 2's own
        health still gates a level-2 exhaustion's retry eligibility."""
        redis = AsyncMock()
        cb = AsyncMock()
        tier_config = ProxyTierConfig(allow_tier1_fallback_for_tier2=False)
        checked_tiers = []

        async def fake_pool_current_state(redis_arg, tier):
            checked_tiers.append(tier)
            return PoolHealthState.HEALTHY

        monkeypatch.setattr(
            "scraper_engine.proxy.dlq_reaper.pool_current_state", fake_pool_current_state
        )
        entry = make_entry(category=FailureCategory.PROXY_EXHAUSTED, level_attempted=2)

        assert await dlq_reaper._is_eligible(entry, redis, cb, tier_config) is True
        assert checked_tiers == [2]

    @pytest.mark.asyncio
    async def test_level3_exhaustion_unaffected_by_tier1_fallback_flag(self, monkeypatch):
        """allow_tier1_fallback_for_tier2 is level-2-specific — a level-3
        exhaustion must not be affected by it, only by its own
        allow_tier2_fallback_for_tier3 flag."""
        redis = AsyncMock()
        cb = AsyncMock()
        tier_config = ProxyTierConfig(allow_tier1_fallback_for_tier2=True)
        checked_tiers = []

        async def fake_pool_current_state(redis_arg, tier):
            checked_tiers.append(tier)
            return PoolHealthState.HEALTHY

        monkeypatch.setattr(
            "scraper_engine.proxy.dlq_reaper.pool_current_state", fake_pool_current_state
        )
        entry = make_entry(category=FailureCategory.PROXY_EXHAUSTED, level_attempted=3)

        assert await dlq_reaper._is_eligible(entry, redis, cb, tier_config) is True
        assert checked_tiers == [3]

    @pytest.mark.asyncio
    async def test_circuit_open_eligible_when_breaker_closed(self):
        redis = AsyncMock()
        cb = AsyncMock()
        tier_config = ProxyTierConfig()
        cb.state.return_value = CircuitState.CLOSED
        entry = make_entry(category=FailureCategory.CIRCUIT_OPEN, url="http://stillbad.com/x")

        assert await dlq_reaper._is_eligible(entry, redis, cb, tier_config) is True
        cb.state.assert_awaited_once_with("stillbad.com")

    @pytest.mark.asyncio
    async def test_circuit_open_not_eligible_when_breaker_still_open(self):
        redis = AsyncMock()
        cb = AsyncMock()
        tier_config = ProxyTierConfig()
        cb.state.return_value = CircuitState.OPEN
        entry = make_entry(category=FailureCategory.CIRCUIT_OPEN)

        assert await dlq_reaper._is_eligible(entry, redis, cb, tier_config) is False

    @pytest.mark.asyncio
    async def test_circuit_open_not_eligible_when_half_open(self):
        """Deliberately conservative — a HALF_OPEN breaker is still probing,
        not confirmed healthy; the reaper waits for a real CLOSED."""
        redis = AsyncMock()
        cb = AsyncMock()
        tier_config = ProxyTierConfig()
        cb.state.return_value = CircuitState.HALF_OPEN
        entry = make_entry(category=FailureCategory.CIRCUIT_OPEN)

        assert await dlq_reaper._is_eligible(entry, redis, cb, tier_config) is False

    @pytest.mark.asyncio
    @pytest.mark.parametrize(("active", "eligible"), [(0, True), (2, False)])
    async def test_politeness_timeout_eligible_only_once_the_domain_is_idle(
        self, active, eligible
    ):
        """Round 65 — POLITENESS_TIMEOUT is contention for this tenant's slots
        on one domain; retrying while the domain still has live holders would
        just time out again."""
        redis = MagicMock()
        redis.raw.eval = AsyncMock(return_value=active)
        entry = make_entry(
            category=FailureCategory.POLITENESS_TIMEOUT,
            url="http://busy.example/p",
            dead_at=datetime.now(UTC) - timedelta(hours=1),
        )
        assert (
            await dlq_reaper._is_eligible(entry, redis, AsyncMock(), ProxyTierConfig())
            is eligible
        )
        key = redis.raw.eval.await_args.args[2]
        assert key == "politeness:turns:test:busy.example"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "category",
        [
            FailureCategory.POLITENESS_TIMEOUT,
            FailureCategory.CAPACITY_TIMEOUT,
            FailureCategory.DEPENDENCY_UNAVAILABLE,
        ],
    )
    async def test_contention_entries_wait_out_an_exponential_backoff(self, category):
        """Round 65 — 60s * 2**auto_retry_count after the last failure."""
        redis = MagicMock()
        redis.raw.eval = AsyncMock(return_value=0)
        fresh = make_entry(category=category, auto_retry_count=1,
                           dead_at=datetime.now(UTC) - timedelta(seconds=100))
        assert await dlq_reaper._is_eligible(fresh, redis, AsyncMock(), ProxyTierConfig()) is False
        assert redis.raw.eval.await_count == 0

    def test_backoff_doubles_per_attempt(self):
        dead = datetime(2026, 1, 1, tzinfo=UTC)
        entry = make_entry(auto_retry_count=2, dead_at=dead)
        assert not dlq_reaper._backoff_elapsed(entry, dead + timedelta(seconds=239))
        assert dlq_reaper._backoff_elapsed(entry, dead + timedelta(seconds=240))

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("in_use", "waiters", "target", "eligible"),
        [(1.0, 0, 4.0, True), (4.0, 0, 4.0, False), (1.0, 2, 4.0, False)],
    )
    async def test_capacity_timeout_needs_spare_host_capacity(
        self, monkeypatch, in_use, waiters, target, eligible
    ):
        from scraper_engine.orchestrator.host_capacity import CapacitySnapshot, HostAdmission

        monkeypatch.setattr(
            HostAdmission,
            "snapshot",
            AsyncMock(return_value=CapacitySnapshot(in_use=in_use, waiters=waiters, target=target)),
        )
        entry = make_entry(category=FailureCategory.CAPACITY_TIMEOUT,
                           dead_at=datetime.now(UTC) - timedelta(hours=1))
        assert (
            await dlq_reaper._is_eligible(entry, MagicMock(), AsyncMock(), ProxyTierConfig())
            is eligible
        )

    @pytest.mark.asyncio
    async def test_dependency_unavailable_is_eligible_once_backed_off(self):
        entry = make_entry(category=FailureCategory.DEPENDENCY_UNAVAILABLE,
                           dead_at=datetime.now(UTC) - timedelta(hours=1))
        assert await dlq_reaper._is_eligible(
            entry, MagicMock(), AsyncMock(), ProxyTierConfig()
        ) is True

    def test_host_capacity_config_is_loaded_once(self):
        dlq_reaper._host_capacity_config.cache_clear()
        assert dlq_reaper._host_capacity_config() is dlq_reaper._host_capacity_config()

    def test_politeness_timeout_is_a_reaped_category(self):
        assert FailureCategory.POLITENESS_TIMEOUT in dlq_reaper._TRANSIENT_CATEGORIES

    @pytest.mark.asyncio
    async def test_unknown_category_is_never_eligible(self):
        redis = AsyncMock()
        cb = AsyncMock()
        tier_config = ProxyTierConfig()
        entry = make_entry(category=FailureCategory.SSRF_BLOCKED)

        assert await dlq_reaper._is_eligible(entry, redis, cb, tier_config) is False


class TestProxyAuthFailed:
    """Round 66 — a proxy refused our credentials. Under paid_only that is
    the account (plan out of traffic): re-drive only once a probe through the
    gateway succeeds, and probe once for many entries. Round 68 — under
    free_first a re-drive goes back to the pool, so pool health decides."""

    @pytest.fixture(autouse=True)
    def _reset_probe(self, monkeypatch):
        monkeypatch.setattr(dlq_reaper, "_gateway_probe", None)

    def _gateway(self, monkeypatch, *, enabled=True, strategy="paid_only", ok=True):
        from scraper_engine.config.schema import DataImpulseConfig

        cfg = DataImpulseConfig(enabled=enabled, strategy=strategy, country="ng", asn=29465)
        monkeypatch.setattr(dlq_reaper, "_dataimpulse_config", lambda: cfg)
        probe = AsyncMock(return_value=ok)
        monkeypatch.setattr(dlq_reaper, "gateway_accepts_credentials", probe)
        return probe

    def test_is_a_reaped_category(self):
        assert FailureCategory.PROXY_AUTH_FAILED in dlq_reaper._TRANSIENT_CATEGORIES

    @pytest.mark.asyncio
    @pytest.mark.parametrize("ok", [True, False])
    async def test_gateway_entry_follows_the_probe(self, monkeypatch, ok):
        probe = self._gateway(monkeypatch, ok=ok)
        entry = make_entry(category=FailureCategory.PROXY_AUTH_FAILED)
        assert (
            await dlq_reaper._is_eligible(entry, AsyncMock(), AsyncMock(), ProxyTierConfig())
            is ok
        )
        probe.assert_awaited_once_with(country="ng", asn=29465)

    @pytest.mark.asyncio
    async def test_one_probe_answers_for_many_entries_until_it_expires(self, monkeypatch):
        probe = self._gateway(monkeypatch, ok=False)
        clock = [1000.0]
        monkeypatch.setattr(dlq_reaper.time, "monotonic", lambda: clock[0])
        entry = make_entry(category=FailureCategory.PROXY_AUTH_FAILED)
        for _ in range(5):
            await dlq_reaper._is_eligible(entry, AsyncMock(), AsyncMock(), ProxyTierConfig())
        assert probe.await_count == 1
        clock[0] += dlq_reaper._GATEWAY_PROBE_TTL_SECONDS
        await dlq_reaper._is_eligible(entry, AsyncMock(), AsyncMock(), ProxyTierConfig())
        assert probe.await_count == 2

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("enabled", "strategy"),
        [(False, "paid_only"), (True, "free_only"), (True, "free_first")],
    )
    async def test_without_a_gateway_only_path_it_checks_tier_health(
        self, monkeypatch, enabled, strategy
    ):
        probe = self._gateway(monkeypatch, enabled=enabled, strategy=strategy)
        monkeypatch.setattr(
            "scraper_engine.proxy.dlq_reaper.pool_current_state",
            AsyncMock(return_value=PoolHealthState.HEALTHY),
        )
        entry = make_entry(category=FailureCategory.PROXY_AUTH_FAILED)
        assert (
            await dlq_reaper._is_eligible(entry, AsyncMock(), AsyncMock(), ProxyTierConfig())
            is True
        )
        probe.assert_not_awaited()

    def test_dataimpulse_config_is_loaded_once(self):
        dlq_reaper._dataimpulse_config.cache_clear()
        assert dlq_reaper._dataimpulse_config() is dlq_reaper._dataimpulse_config()


class TestRateLimited:
    """Round 70 — a URL the site answered 429 at every level is re-driven,
    but late: after the contention backoff (60s * 2**n from dead_at), after
    the site's own Retry-After (retry_not_before), and never into a circuit
    that is open for the domain."""

    @staticmethod
    def _entry(*, dead_ago, not_before_in=None, url="http://slow.example/p"):
        now = datetime.now(UTC)
        return make_entry(
            category=FailureCategory.RATE_LIMITED,
            url=url,
            dead_at=now - timedelta(seconds=dead_ago),
            retry_not_before=(
                now + timedelta(seconds=not_before_in) if not_before_in is not None else None
            ),
        )

    @staticmethod
    def _breaker(state=CircuitState.CLOSED):
        cb = AsyncMock()
        cb.state.return_value = state
        return cb

    def test_is_a_reaped_contention_category(self):
        assert FailureCategory.RATE_LIMITED in dlq_reaper._TRANSIENT_CATEGORIES
        assert FailureCategory.RATE_LIMITED in dlq_reaper._CONTENTION_CATEGORIES

    @pytest.mark.asyncio
    async def test_ineligible_before_the_backoff(self):
        cb = self._breaker()
        entry = self._entry(dead_ago=30)
        assert await dlq_reaper._is_eligible(entry, MagicMock(), cb, ProxyTierConfig()) is False
        cb.state.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_ineligible_before_retry_not_before_even_after_the_backoff(self):
        cb = self._breaker()
        entry = self._entry(dead_ago=3600, not_before_in=600)
        assert await dlq_reaper._is_eligible(entry, MagicMock(), cb, ProxyTierConfig()) is False
        cb.state.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("state", [CircuitState.CLOSED, CircuitState.HALF_OPEN])
    @pytest.mark.parametrize("not_before_in", [None, -60])
    async def test_eligible_after_both_unless_the_circuit_is_open(self, state, not_before_in):
        cb = self._breaker(state)
        entry = self._entry(dead_ago=3600, not_before_in=not_before_in)
        assert await dlq_reaper._is_eligible(entry, MagicMock(), cb, ProxyTierConfig()) is True
        cb.state.assert_awaited_once_with("slow.example")

    @pytest.mark.asyncio
    async def test_ineligible_while_the_circuit_is_open(self):
        cb = self._breaker(CircuitState.OPEN)
        entry = self._entry(dead_ago=3600, not_before_in=-60)
        assert await dlq_reaper._is_eligible(entry, MagicMock(), cb, ProxyTierConfig()) is False


class TestRetryEntry:
    @pytest.mark.asyncio
    async def test_bumps_counter_resets_status_and_reenqueues(self, tenant):
        pg = AsyncMock()
        # UPDATE ... RETURNING succeeded; url_count=5 -> below the 600s
        # floor (round 42's per-URL scaling), so job_timeout stays 600.
        pg.fetchrow.return_value = {"job_id": "job-1", "url_count": 5}
        dlq = AsyncMock()
        queue = MagicMock()  # rq.Queue.enqueue is a sync call
        entry = make_entry(job_id="job-1")

        await dlq_reaper._retry_entry(pg, dlq, tenant, entry, queue)

        dlq.mark_retry_attempt.assert_awaited_once_with(tenant, entry.id)
        queue.enqueue.assert_called_once()
        call = queue.enqueue.call_args
        assert call.args == (
            "scraper_engine.orchestrator.tasks.run_scrape_job",
            "test",
            "job-1",
        )
        assert call.kwargs["job_id"] == "job-1"
        assert call.kwargs["job_timeout"] == 900

    @pytest.mark.asyncio
    async def test_reenqueue_timeout_scales_with_original_job_url_count(self, tenant):
        """Round 42 — live-caught: a retried large batch re-runs
        process_job over the ORIGINAL job's full URL list (cache-hit fast
        path for already-succeeded URLs, but still visited), so it needs
        the same per-URL timeout scaling as the initial POST /v1/scrape
        enqueue, not the flat historical 600s ceiling."""
        pg = AsyncMock()
        pg.fetchrow.return_value = {"job_id": "job-big", "url_count": 51}
        dlq = AsyncMock()
        queue = MagicMock()
        entry = make_entry(job_id="job-big")

        await dlq_reaper._retry_entry(pg, dlq, tenant, entry, queue)

        assert queue.enqueue.call_args.kwargs["job_timeout"] == 51 * 180

    @pytest.mark.asyncio
    async def test_skips_reenqueue_when_job_already_active(self, tenant):
        """A job that's already PENDING/PROCESSING/CANCELLED must not get a
        duplicate rq job stacked on top of it."""
        pg = AsyncMock()
        pg.fetchrow.return_value = None  # UPDATE matched 0 rows (active/cancelled)
        dlq = AsyncMock()
        queue = MagicMock()  # rq.Queue.enqueue is a sync call
        entry = make_entry(job_id="job-2")

        await dlq_reaper._retry_entry(pg, dlq, tenant, entry, queue)

        dlq.mark_retry_attempt.assert_awaited_once()
        queue.enqueue.assert_not_called()

    @pytest.mark.asyncio
    async def test_retry_guard_includes_completed_not_just_failed(self, tenant):
        """Round 37 — a batch job where most URLs succeeded and this one
        didn't settles at status='COMPLETED', not 'FAILED'; the guard must
        cover that case, not just a wholesale job failure. Asserts the
        actual SQL string, since the AsyncMock pg.fetchrow above can't
        catch a WHERE clause that's syntactically fine but semantically
        wrong (it would just always return the same canned value either
        way)."""
        pg = AsyncMock()
        pg.fetchrow.return_value = {"job_id": "job-3", "url_count": 1}
        dlq = AsyncMock()
        queue = MagicMock()
        entry = make_entry(job_id="job-3")

        await dlq_reaper._retry_entry(pg, dlq, tenant, entry, queue)

        sql = pg.fetchrow.call_args.args[1]
        assert "NOT IN ('PENDING', 'PROCESSING', 'CANCELLED')" in sql
        assert "'FAILED', 'DEAD_LETTER'" not in sql


class TestReapTenant:
    @pytest.mark.asyncio
    async def test_only_retries_eligible_candidates(self, tenant, monkeypatch):
        redis = AsyncMock()
        cb = AsyncMock()
        queue = MagicMock()  # rq.Queue.enqueue is a sync call
        cfg = DlqReaperConfig(max_auto_retries=3, batch_size_per_tenant=20)

        eligible = make_entry(job_id="job-eligible")
        ineligible = make_entry(job_id="job-ineligible")

        dlq_instance = AsyncMock()

        async def fake_list_retryable(tenant_arg, categories, max_retries, limit):
            # Round 54 — one category's candidates only, matching the real
            # per-category call shape now that starvation is fixed.
            if categories == [dlq_reaper._TRANSIENT_CATEGORIES[0]]:
                return [eligible, ineligible]
            return []

        dlq_instance.list_retryable = fake_list_retryable
        monkeypatch.setattr(
            "scraper_engine.proxy.dlq_reaper.DeadLetterQueue", lambda pg: dlq_instance
        )

        async def fake_is_eligible(entry, redis_arg, cb_arg, tier_config_arg):
            return entry.job_id == "job-eligible"

        monkeypatch.setattr("scraper_engine.proxy.dlq_reaper._is_eligible", fake_is_eligible)
        retried_jobs = []

        async def fake_retry_entry(pg_arg, dlq_arg, tenant_arg, entry_arg, queue_arg):
            retried_jobs.append(entry_arg.job_id)

        monkeypatch.setattr("scraper_engine.proxy.dlq_reaper._retry_entry", fake_retry_entry)

        pg = AsyncMock()
        tier_config = ProxyTierConfig()
        count = await dlq_reaper._reap_tenant(pg, redis, cb, queue, tenant, cfg, tier_config)

        assert count == 1
        assert retried_jobs == ["job-eligible"]

    @pytest.mark.asyncio
    async def test_one_categorys_backlog_does_not_starve_another(self, tenant, monkeypatch):
        """Round 54 — live-caught: 19 identical stale CIRCUIT_OPEN entries
        for the same never-retried test URL permanently occupied every
        slot of a single combined oldest-first batch, so real
        BROWSER_CRASH/PROXY_EXHAUSTED entries for actual domains never
        even got checked (periodic_dlq_reap_cycle: retried=0 for 10+
        consecutive real cycles). Each category must get its own query, so
        a saturated one can't block the others."""
        redis = AsyncMock()
        cb = AsyncMock()
        queue = MagicMock()
        cfg = DlqReaperConfig(max_auto_retries=3, batch_size_per_tenant=20)

        stale_circuit_open = [
            make_entry(job_id=f"stale-{i}", category=dlq_reaper._TRANSIENT_CATEGORIES[1])
            for i in range(19)
        ]
        real_browser_crash = make_entry(
            job_id="real-crash", category=dlq_reaper._TRANSIENT_CATEGORIES[2]
        )

        dlq_instance = AsyncMock()

        async def fake_list_retryable(tenant_arg, categories, max_retries, limit):
            if categories == [dlq_reaper._TRANSIENT_CATEGORIES[1]]:  # CIRCUIT_OPEN
                return stale_circuit_open
            if categories == [dlq_reaper._TRANSIENT_CATEGORIES[2]]:  # BROWSER_CRASH
                return [real_browser_crash]
            return []

        dlq_instance.list_retryable = fake_list_retryable
        monkeypatch.setattr(
            "scraper_engine.proxy.dlq_reaper.DeadLetterQueue", lambda pg: dlq_instance
        )

        # The stale circuit-open entries never actually become eligible
        # (their circuit never recovers — nothing real ever hits that
        # domain again); the real browser-crash entry's tier is healthy.
        async def fake_is_eligible(entry, redis_arg, cb_arg, tier_config_arg):
            return entry.job_id == "real-crash"

        monkeypatch.setattr("scraper_engine.proxy.dlq_reaper._is_eligible", fake_is_eligible)
        retried_jobs = []

        async def fake_retry_entry(pg_arg, dlq_arg, tenant_arg, entry_arg, queue_arg):
            retried_jobs.append(entry_arg.job_id)

        monkeypatch.setattr("scraper_engine.proxy.dlq_reaper._retry_entry", fake_retry_entry)

        pg = AsyncMock()
        tier_config = ProxyTierConfig()
        count = await dlq_reaper._reap_tenant(pg, redis, cb, queue, tenant, cfg, tier_config)

        assert count == 1
        assert retried_jobs == ["real-crash"]


class TestReapCycle:
    @pytest.mark.asyncio
    async def test_sums_across_tenants_and_isolates_failures(self, monkeypatch):
        from scraper_engine.config.schema import AppConfig

        pg = AsyncMock()
        pg.fetch.return_value = [{"tenant_id": "acme"}, {"tenant_id": "widgets"}]
        redis = AsyncMock()
        cb = AsyncMock()
        queue = MagicMock()  # rq.Queue.enqueue is a sync call
        cfg = AppConfig()

        async def fake_reap_tenant(
            pg_arg, redis_arg, cb_arg, queue_arg, tenant_arg, reaper_cfg, tier_config_arg
        ):
            if str(tenant_arg) == "acme":
                raise RuntimeError("schema unreachable")
            return 2

        monkeypatch.setattr("scraper_engine.proxy.dlq_reaper._reap_tenant", fake_reap_tenant)

        result = await dlq_reaper._reap_cycle(pg, redis, cb, queue, cfg)

        assert result == "retried=2"


class TestDomain:
    def test_extracts_hostname(self):
        assert dlq_reaper._domain("http://example.com:8080/path") == "example.com"

    def test_falls_back_to_unknown_for_unparseable(self):
        assert dlq_reaper._domain("not-a-url") == "unknown"


class TestRun:
    """Daemon lifecycle — mirrors test_harvester_daemon.py::TestRun, same
    supervisor shape (round 34's dlq_reaper reuses that pattern)."""

    @pytest.mark.asyncio
    async def test_wires_from_config_and_shuts_down_cleanly(self, monkeypatch):
        pg = AsyncMock()
        redis = AsyncMock()
        monkeypatch.setattr(dlq_reaper, "PostgresClient", MagicMock(return_value=pg))
        monkeypatch.setattr(dlq_reaper, "RedisClient", MagicMock(return_value=redis))
        monkeypatch.setattr(dlq_reaper, "build_queue", MagicMock(return_value=MagicMock()))

        from scraper_engine.config.schema import AppConfig

        stop = asyncio.Event()
        stop.set()  # request shutdown immediately — exercise start + clean teardown
        await dlq_reaper.run(config=AppConfig(), stop=stop)

        pg.start.assert_awaited_once()
        redis.start.assert_awaited_once()
        pg.stop.assert_awaited_once()
        redis.stop.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_installs_real_signal_handlers_when_stop_not_supplied(self, monkeypatch):
        import os
        import signal as signal_module

        pg = AsyncMock()
        redis = AsyncMock()
        monkeypatch.setattr(dlq_reaper, "PostgresClient", MagicMock(return_value=pg))
        monkeypatch.setattr(dlq_reaper, "RedisClient", MagicMock(return_value=redis))
        monkeypatch.setattr(dlq_reaper, "build_queue", MagicMock(return_value=MagicMock()))

        from scraper_engine.config.schema import AppConfig

        task = asyncio.create_task(dlq_reaper.run(config=AppConfig()))
        await asyncio.sleep(0.1)  # let run() reach add_signal_handler before we fire one
        os.kill(os.getpid(), signal_module.SIGTERM)
        await asyncio.wait_for(task, timeout=5)

        pg.stop.assert_awaited_once()
        redis.stop.assert_awaited_once()


class TestMain:
    def test_main_drives_run_via_asyncio_run(self, monkeypatch):
        calls = {"n": 0}

        async def fake_run():
            calls["n"] += 1

        monkeypatch.setattr(dlq_reaper, "run", fake_run)
        dlq_reaper.main()
        assert calls["n"] == 1
