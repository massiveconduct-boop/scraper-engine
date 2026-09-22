# proxy/dlq_reaper.py
"""Auto-retries transient DLQ entries once their underlying condition has
cleared (round 34).

This module's own _TRANSIENT_CATEGORIES (PROXY_EXHAUSTED, CIRCUIT_OPEN,
and — round 42 — BROWSER_CRASH, NETWORK_TIMEOUT) describe failures that
resolve once *external* state changes — unlike orchestrator/worker.py's
PERMANENT_FAILURE_CATEGORIES, retrying them isn't futile, it just has to
wait for the right moment. Before round 34, a DLQ'd job sat there forever
until a human noticed and manually retried it; there was no automated path
at all (storage/dlq.py's old `retry()` had zero callers). Deliberately its
own list, not worker.py's TRANSIENT_FAILURE_CATEGORIES — that set also
feeds worker.py's DLQ_ELIGIBLE_CATEGORIES, which gates whether a mid-
escalation failure breaks early instead of trying the next level; this
reaper only ever sees entries that already exhausted every level, so its
own eligibility set can be broader without touching escalation behavior.

This is a poll-driven check against current state (proxy/pool_health.py's
persisted per-tier state for PROXY_EXHAUSTED/BROWSER_CRASH/NETWORK_TIMEOUT,
CircuitBreaker.state() for CIRCUIT_OPEN) rather than a push-only trigger —
the same belt-and-suspenders choice proxy/harvester_daemon.py's kick
watcher makes, for the same reason: a push signal can be missed on daemon
restart, a poll against current truth can't be.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import logging
import signal
from datetime import UTC, datetime, timedelta
from urllib.parse import urlparse

from rq import Queue

from scraper_engine.config.loader import load_config
from scraper_engine.config.schema import (
    AppConfig,
    DlqReaperConfig,
    HostCapacityConfig,
    ProxyTierConfig,
)
from scraper_engine.core.host_identity import resolve_host_id
from scraper_engine.core.models import FailureCategory
from scraper_engine.core.periodic import run_periodic
from scraper_engine.core.tenant import TenantId
from scraper_engine.observability.bootstrap import bootstrap_observability
from scraper_engine.orchestrator.circuit_breaker import CircuitBreaker, CircuitState
from scraper_engine.orchestrator.host_capacity import HostAdmission
from scraper_engine.orchestrator.job_queue import build_queue
from scraper_engine.orchestrator.politeness import PolitenessController
from scraper_engine.proxy.pool_health import current_state as pool_current_state
from scraper_engine.storage.dlq import DeadLetterEntry, DeadLetterQueue
from scraper_engine.storage.postgres_client import PostgresClient
from scraper_engine.storage.redis_client import RedisClient

logger = logging.getLogger(__name__)

_SCRAPE_JOB_TIMEOUT_SECONDS = 600
# Round 42 — same per-URL scaling constant as api/routes.py's
# _PER_URL_TIMEOUT_SECONDS (kept as a separate constant, not a shared
# import, to avoid a daemon-module -> API-module dependency for one int).
# Round 46 — bumped 60->120 alongside api/routes.py's constant; same root
# cause (60s/URL never actually covered L1+L2+L3's own 120s timeout sum),
# same fix, kept in sync manually since these stay deliberately separate.
# Round 63 — 120->180, again alongside api/routes.py (measured: worst
# per-URL wall time 156s once politeness slot waits are counted).
_PER_URL_TIMEOUT_SECONDS = 180
# Round 42 — BROWSER_CRASH/NETWORK_TIMEOUT joined this reaper-local list
# (deliberately NOT orchestrator/worker.py's own TRANSIENT_FAILURE_CATEGORIES,
# which also feeds DLQ_ELIGIBLE_CATEGORIES and gates early-break-vs-escalate
# inside the per-level loop there — adding these categories to THAT set would
# stop a mid-escalation BROWSER_CRASH/NETWORK_TIMEOUT from ever reaching L2/L3
# at all, the opposite of round 37's intent). This list only controls which
# terminal (all-levels-exhausted) DLQ entries the reaper considers for
# auto-retry, once worker.py's for/else branch stopped mislabeling every such
# entry as PROXY_EXHAUSTED and started reporting the real last-level failure
# (see that branch's comment). BROWSER_CRASH/NETWORK_TIMEOUT are exactly the
# categories _PROXY_RETRYABLE_CATEGORIES already documents as proxy-
# attributable, not page/content issues — the same reasoning that justifies a
# same-level retry there justifies this reaper auto-retrying them too, once
# the relevant tier's pool health recovers (see _is_eligible below).
_TRANSIENT_CATEGORIES = [
    FailureCategory.PROXY_EXHAUSTED,
    FailureCategory.CIRCUIT_OPEN,
    FailureCategory.BROWSER_CRASH,
    FailureCategory.NETWORK_TIMEOUT,
    # Round 65 — worker.py has always listed POLITENESS_TIMEOUT as transient
    # and "auto-retry eligible", but it was missing HERE, the only list the
    # reaper actually selects from, so no politeness-timeout DLQ entry was
    # ever retried. It is contention for this tenant's slots on one domain,
    # so it is eligible once that domain has no live slot holder left.
    FailureCategory.POLITENESS_TIMEOUT,
    # Round 65 — host admission (orchestrator/host_capacity.py): our own
    # browser capacity ran out, or our own Redis failed. See _is_eligible.
    FailureCategory.CAPACITY_TIMEOUT,
    FailureCategory.DEPENDENCY_UNAVAILABLE,
]

# Round 65 — contention categories wait base * 2**auto_retry_count after
# their last failure before a retry: re-driving the moment a slot or seat
# frees would re-enter the same contention at once, and a re-drive re-runs a
# whole job on a worker.
_CONTENTION_CATEGORIES = frozenset(
    {
        FailureCategory.POLITENESS_TIMEOUT,
        FailureCategory.CAPACITY_TIMEOUT,
        FailureCategory.DEPENDENCY_UNAVAILABLE,
    }
)
_CONTENTION_BACKOFF_BASE_SECONDS = 60.0


@functools.cache
def _host_capacity_config() -> HostCapacityConfig:
    return load_config().host_capacity


def _backoff_elapsed(entry: DeadLetterEntry, now: datetime | None = None) -> bool:
    wait = _CONTENTION_BACKOFF_BASE_SECONDS * (2**entry.auto_retry_count)
    return (now or datetime.now(UTC)) >= entry.dead_at + timedelta(seconds=wait)


def _domain(url: str) -> str:
    return urlparse(url).hostname or "unknown"


async def _is_eligible(
    entry: DeadLetterEntry,
    redis: RedisClient,
    circuit_breaker: CircuitBreaker,
    tier_config: ProxyTierConfig,
) -> bool:
    """PROXY_EXHAUSTED, BROWSER_CRASH, and NETWORK_TIMEOUT (round 42 — the
    latter two joined this check once worker.py's for/else terminal branch
    stopped mislabeling every all-levels-exhausted failure as
    PROXY_EXHAUSTED, see _TRANSIENT_CATEGORIES above) are eligible once
    their tier (level_attempted maps 1:1 to a proxy/pool_health.py tier) is
    no longer DEGRADED/CRITICAL. CIRCUIT_OPEN is eligible once the breaker
    has fully closed for that domain — checked via the pure-read state()
    rather than allow_request(), which would itself consume a HALF_OPEN
    probe slot meant for real traffic, not the reaper's own bookkeeping.

    Round 37 — level_attempted==3 checks tier 2's health instead when
    allow_tier2_fallback_for_tier3 is enabled, not tier 3's own. Live-caught:
    round 33's tier-2-fallback flag exists specifically because free proxy
    sources structurally can't reach tier 3's raw 90+ score threshold
    (round 33's own documented finding) — proxy/pool_health.py's tier-3
    count is therefore permanently CRITICAL under this config regardless of
    whether a level-3 lease actually succeeds (round 37's live testing
    measured ~87% real success on level-3 leases via the tier-2 fallback,
    while tier 3's raw pool_health sat CRITICAL the entire time). Checking
    raw tier-3 health here made every level-3-exhaustion DLQ entry
    permanently ineligible for auto-retry — silently defeating the exact
    mechanism round 34 built to heal transient proxy exhaustion, for
    exactly the deployment shape (free-proxy-only) this repo already runs
    in. Checking tier 2 instead reflects what actually gates a retry's
    success under this config.

    Round 39 — same substitution, one tier down: level_attempted==2 checks
    tier 1's health instead when allow_tier1_fallback_for_tier2 is enabled.
    Same live shape as round 37 but for tier 2 this time — the round-39
    scoring fixes that corrected years of inflated reliability_score values
    back to their real, honest ones also dropped tier 2's real supply
    below critical_below_count, so tier 2's raw pool_health now sits
    CRITICAL regardless of whether a level-2 lease actually succeeds via
    the new tier-1 fallback."""
    from scraper_engine.proxy.pool_health import PoolHealthState

    if entry.failure_category in (
        FailureCategory.PROXY_EXHAUSTED,
        # Round 42 — same tier-health check as PROXY_EXHAUSTED. These
        # reach the DLQ only via worker.py's for/else terminal branch
        # (every level failed, non-DLQ-eligible category at each), and
        # _PROXY_RETRYABLE_CATEGORIES already treats them as proxy-
        # attributable rather than target/content issues — a recovered
        # tier is exactly the condition that makes a retry plausible.
        FailureCategory.BROWSER_CRASH,
        FailureCategory.NETWORK_TIMEOUT,
    ):
        check_tier = entry.level_attempted
        if entry.level_attempted == 3 and tier_config.allow_tier2_fallback_for_tier3:
            check_tier = 2
        elif entry.level_attempted == 2 and tier_config.allow_tier1_fallback_for_tier2:
            check_tier = 1
        pool_state = await pool_current_state(redis, check_tier)
        return pool_state == PoolHealthState.HEALTHY
    if entry.failure_category == FailureCategory.CIRCUIT_OPEN:
        circuit_state = await circuit_breaker.state(_domain(entry.url))
        return circuit_state == CircuitState.CLOSED
    if entry.failure_category in _CONTENTION_CATEGORIES and not _backoff_elapsed(entry):
        return False
    if entry.failure_category == FailureCategory.CAPACITY_TIMEOUT:
        # Only once this host has spare browser capacity: nobody waiting and
        # units free. Re-driving into a saturated host is exactly the
        # overload the URL timed out on.
        snap = await HostAdmission(
            redis.raw, resolve_host_id(), _host_capacity_config()
        ).snapshot()
        return snap.waiters == 0 and snap.in_use < snap.target
    if entry.failure_category == FailureCategory.DEPENDENCY_UNAVAILABLE:
        # The reaper reaching this point means Redis answers again.
        return True
    if entry.failure_category == FailureCategory.POLITENESS_TIMEOUT:
        politeness = PolitenessController(redis.raw)
        active = await politeness.active_slots(_domain(entry.url), TenantId(entry.tenant_id))
        return active == 0
    return False


async def _retry_entry(
    pg: PostgresClient,
    dlq: DeadLetterQueue,
    tenant: TenantId,
    entry: DeadLetterEntry,
    queue: Queue,
) -> None:
    """Bump the retry counter, reset the job back to PENDING (only if it's
    not already active — a job can have multiple DLQ'd URLs, and the first
    eligible one to be retried shouldn't stomp a job an unrelated cause
    already re-activated), and re-enqueue under the same job_id. Reusing the
    original job_id (rather than minting a new one) means a caller polling
    GET /v1/jobs/{job_id} keeps seeing the same job transition PENDING ->
    PROCESSING -> terminal again, instead of the retry becoming invisible
    under a different id. Worker.process_job's cache check (CACHE_TTL_DAYS)
    means URLs that already succeeded are served from cache, not re-fetched
    — only the still-failing URL(s) actually do real work again.

    Round 37 — the guard was `status IN ('FAILED', 'DEAD_LETTER')`, but
    `worker.py::process_job` never actually sets the job-level status to
    'DEAD_LETTER' (that value only ever appears in this docstring's own
    state diagram, not in the real status computation — grepped,
    confirmed), and a batch job with a partial failure (some URLs
    succeeded, this one didn't) settles at 'COMPLETED', not 'FAILED'
    (`any_success=True` -> COMPLETED, per JobStatusResponse's own logic).
    Live-caught against real production data from this session: a real
    47-URL batch's DLQ'd URLs sat with `auto_retry_count=0` because their
    job's status was 'COMPLETED', so this guard matched zero rows every
    reap cycle, silently no-opping the retry even when _is_eligible said
    yes. The common real-world case (a big batch where most URLs succeed
    and a few don't) was therefore NEVER actually auto-retried, regardless
    of the eligibility fix above. Broadened to exclude only genuinely
    active (PENDING/PROCESSING) or intentionally terminal (CANCELLED)
    jobs — every other status is fair game for re-activating this one
    still-failing URL."""
    await dlq.mark_retry_attempt(tenant, entry.id)
    row = await pg.fetchrow(
        tenant,
        """UPDATE scrape_jobs SET status = 'PENDING', updated_at = NOW()
           WHERE job_id = $1::uuid AND status NOT IN ('PENDING', 'PROCESSING', 'CANCELLED')
           RETURNING job_id, array_length(urls, 1) AS url_count""",
        entry.job_id,
    )
    if row is None:
        # Job is already PENDING/PROCESSING/COMPLETED/CANCELLED for some
        # other reason — don't re-enqueue a duplicate rq job on top of it.
        return
    # Round 42 — same scaling as api/routes.py's POST /v1/scrape enqueue.
    # A retry re-runs process_job over the ORIGINAL job's full URL list
    # (already-succeeded URLs hit the CACHE_TTL_DAYS cache fast path, but
    # the loop still visits every one of them), so a large original batch
    # needs the same generous per-URL budget here, not the flat historical
    # ceiling — otherwise a retried large job hits the exact same hard-kill
    # / stuck-at-PROCESSING failure mode this round root-caused.
    url_count = row["url_count"] or 1
    queue.enqueue(
        "scraper_engine.orchestrator.tasks.run_scrape_job",
        str(tenant),
        entry.job_id,
        job_id=entry.job_id,
        job_timeout=max(_SCRAPE_JOB_TIMEOUT_SECONDS, url_count * _PER_URL_TIMEOUT_SECONDS),
    )
    logger.info(
        "dlq_auto_retry job_id=%s url=%s category=%s attempt=%d",
        entry.job_id,
        entry.url,
        entry.failure_category.value,
        entry.auto_retry_count + 1,
    )


async def _reap_tenant(
    pg: PostgresClient,
    redis: RedisClient,
    circuit_breaker: CircuitBreaker,
    queue: Queue,
    tenant: TenantId,
    cfg: DlqReaperConfig,
    tier_config: ProxyTierConfig,
) -> int:
    """Round 54 — one combined `list_retryable(tenant, _TRANSIENT_CATEGORIES,
    ..., limit=batch_size_per_tenant)` call used to select the batch's
    entries globally oldest-first across ALL categories, not per category.
    Live-caught against real research_agent data: 19 identical CIRCUIT_OPEN
    entries for the same test URL (dead since 2026-08-13, never eligible —
    that domain's circuit never actually recovers because nothing real
    ever hits it again) permanently occupied every one of the batch's 20
    slots, every single cycle, since they're always the oldest. Real
    BROWSER_CRASH/PROXY_EXHAUSTED/NETWORK_TIMEOUT/CIRCUIT_OPEN entries for
    actual domains — some plausibly eligible right then (tier pool state
    was HEALTHY) — never even got checked; `periodic_dlq_reap_cycle:
    retried=0` for 10+ consecutive real cycles was the live symptom, not a
    coincidence. Querying each category separately, each with its own full
    `batch_size_per_tenant` budget, means one category's backlog (however
    large, however permanently stuck) can never starve the others."""
    dlq = DeadLetterQueue(pg)
    retried = 0
    for category in _TRANSIENT_CATEGORIES:
        candidates = await dlq.list_retryable(
            tenant, [category], cfg.max_auto_retries, limit=cfg.batch_size_per_tenant
        )
        for entry in candidates:
            if await _is_eligible(entry, redis, circuit_breaker, tier_config):
                await _retry_entry(pg, dlq, tenant, entry, queue)
                retried += 1
    return retried


async def _reap_cycle(
    pg: PostgresClient,
    redis: RedisClient,
    circuit_breaker: CircuitBreaker,
    queue: Queue,
    cfg: AppConfig,
) -> str:
    """One reap cycle across every real tenant (not 'system' — DLQ entries
    belong to real tenants' jobs, unlike webhook_outbox's pool-health rows).
    One tenant's schema being unreachable must not block the others, same
    isolation contract as observability/metrics.py::refresh_dlq_size."""
    system = TenantId("system")
    rows = await pg.fetch(system, "SELECT tenant_id FROM public.tenants")
    total_retried = 0
    for row in rows:
        tenant = TenantId(row["tenant_id"])
        try:
            total_retried += await _reap_tenant(
                pg, redis, circuit_breaker, queue, tenant, cfg.dlq_reaper, cfg.proxy_tiers
            )
        except Exception:
            logger.exception("dlq_reaper_tenant_failed tenant=%s", tenant)
    return f"retried={total_retried}"


async def run(config: AppConfig | None = None, stop: asyncio.Event | None = None) -> None:
    """Start the reap loop and block until a stop signal arrives.

    ``stop`` lets a caller (or a test) drive shutdown directly; when omitted
    the daemon installs SIGTERM/SIGINT handlers so ``docker compose stop``
    is graceful — same shape as proxy/harvester_daemon.py::run.
    """
    cfg = config or load_config()
    bootstrap_observability(cfg.observability)

    pg = PostgresClient(cfg.storage.database_url)
    await pg.start()
    redis = RedisClient(redis_url=cfg.storage.redis_url)
    await redis.start()
    circuit_breaker = CircuitBreaker(
        redis.raw,
        failure_threshold=cfg.circuit_breaker.failure_threshold,
        attempt_threshold=cfg.circuit_breaker.attempt_threshold,
        cooldown_seconds=cfg.circuit_breaker.cooldown_seconds,
        max_cooldown_seconds=cfg.circuit_breaker.max_cooldown_seconds,
        failure_streak_ttl_seconds=cfg.circuit_breaker.failure_streak_ttl_seconds,
    )
    queue = build_queue(cfg.storage.redis_url)

    task = asyncio.create_task(
        run_periodic(
            "dlq_reap",
            lambda: _reap_cycle(pg, redis, circuit_breaker, queue, cfg),
            cfg.dlq_reaper.interval_seconds,
            redis=redis,
        )
    )
    logger.info("dlq reaper started (interval=%ss)", cfg.dlq_reaper.interval_seconds)

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
        logger.info("dlq reaper stopping")
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await redis.stop()
        await pg.stop()
        logger.info("dlq reaper stopped cleanly")


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":  # pragma: no cover — only true under `python -m`, not tests
    main()
