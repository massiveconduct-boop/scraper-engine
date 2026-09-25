# orchestrator/worker.py
"""RQ task definition, execution loop, state machine driver.

The worker dequeues jobs and drives the escalation state machine:
  PENDING → CIRCUIT_CHECK → FETCHING_L1 → PARSING_L1
                                          ↘ failure → ESCALATING_L2 → ...
                                                                       ↘ DEAD_LETTER
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError

from scraper_engine.core.budget import start_display_wait_meter
from scraper_engine.core.models import FailureCategory, FetchResult, JobStatus, JobStatusResponse
from scraper_engine.fetcher._failure import classify_http_status
from scraper_engine.observability.metrics import fetch_duration_seconds
from scraper_engine.orchestrator.host_capacity import (
    AdmissionCancelledError,
    AdmissionTimeoutError,
    AdmissionUnavailableError,
)

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from pydantic import HttpUrl

    from scraper_engine.browser.botasaurus_pool import BotasaurusPool
    from scraper_engine.browser.pool import BrowserPool
    from scraper_engine.config.schema import AppConfig
    from scraper_engine.core.models import ConfigOverrides, Proxy, ScrapeRequest
    from scraper_engine.core.tenant import TenantId
    from scraper_engine.storage.dlq import DeadLetterQueue
    from scraper_engine.storage.postgres_client import PostgresClient
    from scraper_engine.storage.redis_client import RedisClient

    from .circuit_breaker import CircuitBreaker
    from .host_capacity import HostAdmission
    from .politeness import PolitenessController

LEVELS = [1, 2, 3]

# Round 37 — a proxy that clears the lease-time preflight (proxy/net_probe.py)
# can still fail once handed to the real browser fetch, for reasons the
# preflight can't predict (e.g. Camoufox's own internal geoip IP-lookup
# hitting a different, unrelated third-party endpoint than the preflight
# checks, and failing there specifically — live-caught, see
# technical-debt.md's round-37 entry). Before this, one such proxy burned
# the ENTIRE level (get_proxy() is only called once per level in
# _fetch_url) even though dozens of other viable proxies existed in the
# pool. These two categories are the ones live evidence tied to a bad
# proxy specifically, not the target site or the page content itself —
# retrying with a fresh lease is only correct for failures the proxy
# itself plausibly caused.
_PROXY_RETRYABLE_CATEGORIES = frozenset(
    {FailureCategory.BROWSER_CRASH, FailureCategory.NETWORK_TIMEOUT}
)
_SAME_LEVEL_PROXY_RETRIES = 1  # one retry with a fresh proxy before giving up on this level

# Round 62 — DETECTION_BLOCK is retryable too, but ONLY on the paid gateway,
# and only because round 62 gave the gateway a way to present a genuinely
# different exit IP on the next attempt (proxy/paid_gateway.py's sessid
# parameter). It stays out of _PROXY_RETRYABLE_CATEGORIES above because
# that set governs the free pool as well, where a block is far more often
# the target fingerprinting the request than the IP, and where a wasted
# retry costs a second full browser render for nothing.
#
# Live evidence this closes (ops/research/itel-30000mah-jumia,
# 20 Sep 2026): the first Jumia catalog fetch through the gateway returned
# 200, every subsequent one returned a Cloudflare 403 at L1, L2 AND L3.
# DETECTION_BLOCK not being retryable here meant the engine accepted that
# first 403 as final, and — since build_gateway_proxy() then produced one
# fixed username — even process_job's gateway-fallback re-attempt went out
# over the same, already-flagged exit identity. The pool never changed IP
# because nothing in the system could ask it to.
_GATEWAY_ROTATE_CATEGORIES = frozenset({FailureCategory.DETECTION_BLOCK})

# Round 68 — why a URL did not go out through the paid gateway while it is
# refusing our credentials (proxy/gateway_health.py). Ends up in the result's
# and DLQ entry's error_message, so it says what the operator must do.
_GATEWAY_REFUSED_MESSAGE = (
    "paid gateway is refusing our credentials (plan out of traffic or bad login) "
    "— not attempted; top up or fix DATAIMPULSE_* and it is re-tested automatically"
)
# Round 69 — research_agent detects the gateway note by this text; keep it
# stable (and a prefix of _GATEWAY_REFUSED_MESSAGE) or tell them the new one.
_GATEWAY_REFUSED_MARKER = "paid gateway is refusing our credentials"


def _note_gateway_skipped(result: FetchResult, skipped: bool) -> None:
    """Round 69 — on a URL's terminal failure, say that the paid gateway was
    wanted on its path and skipped because it is refusing our credentials:
    flag the result and append the refusal note to its message, once."""
    if not skipped:
        return
    result.paid_gateway_skipped = True
    message = result.error_message or ""
    if _GATEWAY_REFUSED_MARKER in message:
        return
    result.error_message = (
        f"{message} ({_GATEWAY_REFUSED_MESSAGE})" if message else _GATEWAY_REFUSED_MESSAGE
    )


# Round 69 — what each "blocked" status means to a reader. DETECTION_BLOCK
# covers all of them (fetcher/_failure.py::_DETECTION_BLOCK_STATUSES).
_BLOCK_STATUS_MEANING = {
    401: "unauthorized",
    403: "refused",
    404: "not found, or a block shaped like one",
    405: "method refused",
    410: "gone, or a block shaped like it",
    429: "rate limited",
}


def _describe_block(terminal: FetchResult, last: FetchResult) -> None:
    """Round 69 — start a terminal DETECTION_BLOCK's message with what the
    block was, where and through which route: `HTTP 403 (refused) at L3 via
    pool — <original message>`, and set `block_reason` in escalations'
    vocabulary. One category covers 401/403/404/405/410/429 and challenge
    pages; a reader could not tell a rate limit from a bot check."""
    reason = last.block_reason or (f"status:{last.http_status}" if last.http_status else None)
    terminal.block_reason = reason
    if reason is None:
        what = "blocked"
    elif reason.startswith("status:"):
        status = int(reason.removeprefix("status:"))
        meaning = _BLOCK_STATUS_MEANING.get(status)
        what = f"HTTP {status} ({meaning})" if meaning else f"HTTP {status}"
    elif reason.startswith("signature:"):
        what = f"challenge page (signature '{reason.removeprefix('signature:')}')"
    elif reason == "js_gated":
        what = "JavaScript-gated page (no content rendered)"
    else:
        what = f"blocked page ({reason})"
    route = last.proxy_source or "direct"
    summary = f"{what} at L{last.level_used} via {route}"
    terminal.error_message = (
        f"{summary} — {terminal.error_message}" if terminal.error_message else summary
    )


# round 29 — how long a successful scrape_results row is considered a valid
# cache hit before it must be re-scraped. Sliding: a hit refreshes freshness
# by inserting a new row with a fresh extracted_at (see _persist_one_result
# in orchestrator/tasks.py), not a fixed clock from the very first scrape.
# Must stay <= S3Client.SUCCESS_RETENTION_DAYS, otherwise a cache hit could
# reference an html_snapshot_url whose S3 object has already expired.
CACHE_TTL_DAYS = 7

# Failure taxonomy split (round 34) — both sets still land in the DLQ (still
# visible, still auditable via GET /v1/jobs/{id}/dlq), but only TRANSIENT
# categories are eligible for proxy/dlq_reaper.py's auto-retry. PERMANENT
# categories describe conditions that retrying can never fix (a blocked
# SSRF target stays blocked, an exceeded quota doesn't refill itself mid-job,
# a dead host doesn't start resolving) — auto-retrying those would just burn
# proxy/browser budget for a guaranteed repeat failure. TRANSIENT categories
# describe conditions that resolve once *external* state changes: the proxy
# pool refills (proxy/pool_health.py's recovered transition) or a circuit
# breaker's cooldown expires — previously PROXY_EXHAUSTED was grouped with
# the permanent set even though it's transient by nature, so a DLQ'd job
# never got retried until a human noticed and hit the retry endpoint by hand.
PERMANENT_FAILURE_CATEGORIES = frozenset(
    {
        FailureCategory.SSRF_BLOCKED,
        FailureCategory.QUOTA_EXCEEDED,
        FailureCategory.HOST_UNREACHABLE,
        # Round 43 gave 404 its own always-permanent, circuit-exempt
        # NOT_FOUND on the assumption a definitive "not found" status could
        # only mean a genuinely dead URL. Round 45 found that wrong — live-
        # verified two of this deployment's own target domains return a 404-
        # shaped response for what's actually a Cloudflare bot-management
        # block, and the same URLs load fine in a real browser. 404 now
        # escalates like any other block status (see
        # ChallengeDetector.CHALLENGE_STATUS_CODES); NOT_FOUND is kept here
        # only so any already-persisted historical DLQ/result rows using
        # this category (from round 43-44) still resolve as non-retryable —
        # nothing assigns it going forward.
        FailureCategory.NOT_FOUND,
    }
)
TRANSIENT_FAILURE_CATEGORIES = frozenset(
    {
        FailureCategory.PROXY_EXHAUSTED,
        FailureCategory.CIRCUIT_OPEN,
        # Round 63 — pure contention: the URL never got a politeness slot.
        # Transient by construction, since what clears it is sibling URLs
        # finishing, so it is auto-retry eligible like the other two.
        FailureCategory.POLITENESS_TIMEOUT,
        # Round 65 — host admission (orchestrator/host_capacity.py): our own
        # browser capacity or our own Redis, never the target. See
        # core/models.py for why neither may touch the circuit or level memory.
        FailureCategory.CAPACITY_TIMEOUT,
        FailureCategory.DEPENDENCY_UNAVAILABLE,
    }
)


@dataclass
class _RenderAdmission:
    """One URL's inputs to a host-capacity claim (round 65), threaded from
    process_job down to every render _fetch_with_proxy makes for it.

    priority_ms is the URL's FIRST enqueue time and never changes, so a URL
    keeps its place in the host's line across levels, pool retries, gateway
    rotations and the gateway retry — escalating must not send it to the back.
    """

    tenant_id: TenantId
    domain: str
    concurrency: int
    delay_seconds: float
    priority_ms: int
    deadline: float  # time.monotonic() after which no new wait may start
    is_cancelled: Callable[[], Awaitable[bool]]
    timings: dict[str, int]

    def wait_budget(self) -> float:
        return max(0.0, self.deadline - time.monotonic())


DLQ_ELIGIBLE_CATEGORIES = PERMANENT_FAILURE_CATEGORIES | TRANSIENT_FAILURE_CATEGORIES


class Worker:
    """RQ worker: dequeues jobs, drives the escalation state machine."""

    def __init__(
        self,
        redis: RedisClient,
        circuit_breaker: CircuitBreaker,
        politeness: PolitenessController,
        dlq: DeadLetterQueue,
        config: AppConfig | None = None,
        pg: PostgresClient | None = None,
        browser_pool: BrowserPool | None = None,
        botasaurus_pool: BotasaurusPool | None = None,
        admission: HostAdmission | None = None,
    ) -> None:
        self._redis = redis
        # Round 65 — the host-wide browser budget. None (host_capacity
        # disabled) keeps the per-process politeness-slot path unchanged.
        self._admission = admission
        self._circuit_breaker = circuit_breaker
        self._politeness = politeness
        self._dlq = dlq
        self._pg = pg
        # None keeps pre-round-25 behavior (fetchers cold-start their own
        # CamoufoxWrapper). One pool per job process — see orchestrator/tasks.py.
        self._browser_pool = browser_pool
        # None keeps every Botasaurus fetch one-shot. One pool per job process
        # (round 26), same lifetime as browser_pool — see orchestrator/tasks.py.
        self._botasaurus_pool = botasaurus_pool
        # config drives fetcher construction via fetcher/factory.py. Loaded once
        # here (not per-fetch) so production.yaml values are authoritative for
        # every fetch this worker dispatches. Falls back to load_config() so
        # callers that don't pass one still get YAML-driven fetchers, never
        # bare constructor defaults.
        if config is None:
            from scraper_engine.config.loader import load_config

            config = load_config()
        self._config = config
        # Round 40 — fail fast, not silently mid-job. RQ forks one process per
        # job, so raising here fails only the job(s) this process would have
        # handled, loudly and immediately, instead of every _fetch_with_proxy
        # call quietly falling back to the free pool (or worse, crashing deep
        # inside the retry loop) because the toggle was flipped on without the
        # gateway env vars actually being set.
        if self._config.dataimpulse.enabled:
            from scraper_engine.proxy.paid_gateway import build_gateway_proxy

            if build_gateway_proxy() is None:
                raise RuntimeError(
                    "config.dataimpulse.enabled=true but one or more of "
                    "DATAIMPULSE_PROXY_HOST / DATAIMPULSE_PORT / "
                    "DATAIMPULSE_USERNAME / DATAIMPULSE_PASSWORD is not set "
                    "in the environment"
                )
        # One ChallengeDetector for escalation decisions (challenge pages and
        # JS-gated shells) — same single source of truth the fetchers use.
        from scraper_engine.fetcher.challenge_detector import ChallengeDetector

        self._challenge_detector = ChallengeDetector()
        # Build the CAPTCHA solver once (env keys + per-tenant budget on Redis)
        # and thread it into the browser fetchers via the factory. None when no
        # provider key is set — solving stays disabled, fetch still runs. Built
        # here (not per-fetch) for the same reason config is: one authoritative
        # construction site (round 20 — wires services/captcha_solver in).
        from scraper_engine.core.budget import CapSolverBudget
        from scraper_engine.services.captcha_solver import build_captcha_solver

        self._captcha_solver = build_captcha_solver(CapSolverBudget(self._redis, pg=self._pg))
        # Built once here (not per-fetch), same rationale as captcha_solver
        # above. None when neither FIRECRAWL_API_KEY nor FIRECRAWL_BASE_URL
        # is set — markdown conversion is simply skipped (round 29 — moved
        # out of Level1Fetcher so it applies to whichever level succeeds,
        # not just L1).
        from scraper_engine.services.firecrawl_client import build_firecrawl_client

        self._firecrawl = build_firecrawl_client()
        # Built once here, same rationale as firecrawl/captcha_solver above. None
        # when EXTRACTION_ENGINE_BASE_URL isn't set — schema-driven extraction
        # simply falls back to AdaptiveSelector (see process_job below).
        from scraper_engine.services.extraction_engine_client import (
            build_extraction_engine_client,
        )

        self._extraction_engine = build_extraction_engine_client()
        # Round 63 — per-domain "start the ladder here" memory. Reads and
        # writes are best-effort (see level_memory.py): a Redis problem costs
        # the latency this was built to save, never a job.
        from .level_memory import LevelMemory

        self._level_memory = LevelMemory(redis, self._config.escalation)
        # Round 68 — the shared "gateway is refusing our credentials" verdict
        # (proxy/gateway_health.py). One 407 takes the gateway out of use for
        # every worker until the verdict expires.
        from scraper_engine.proxy.gateway_health import GatewayHealth

        self._gateway_health = GatewayHealth(redis, self._config.dataimpulse.refused_ttl_seconds)

    def _resolve_levels(self, overrides: ConfigOverrides | None) -> list[int]:
        """The escalation ladder for this request, narrowed by min/max_level.

        Round 63. Always non-empty: ConfigOverrides already rejects
        min_level > max_level at validation time, and both are constrained to
        the 1..3 range LEVELS spans.
        """
        if overrides is None:
            return list(LEVELS)
        low = overrides.min_level or LEVELS[0]
        high = overrides.max_level or LEVELS[-1]
        return [level for level in LEVELS if low <= level <= high]

    def _resolve_politeness(self, overrides: ConfigOverrides | None) -> tuple[int, float]:
        """This request's (concurrency, delay_seconds), clamped to the
        operator's ceilings.

        Round 63 — the limits used to be construction-time scalars a caller
        could not reach at all, which meant a trusted bulk crawl of one
        domain ran at the same 2-concurrent/5s-apart pace as an untrusted
        scrape of a stranger's site. A caller may now trade politeness for
        throughput, but only inside config.politeness's
        max_request_concurrency / min_request_delay_seconds — the clamp is
        server-side precisely because the request is the untrusted half of
        this decision.
        """
        cfg = self._config.politeness
        concurrency = cfg.default_concurrency
        delay = cfg.default_delay_seconds
        if overrides is not None:
            if overrides.politeness_concurrency is not None:
                concurrency = min(overrides.politeness_concurrency, cfg.max_request_concurrency)
            if overrides.politeness_delay_seconds is not None:
                delay = max(overrides.politeness_delay_seconds, cfg.min_request_delay_seconds)
        return concurrency, delay

    def _clamp_timeout(self, request: ScrapeRequest) -> ScrapeRequest:
        """Cap the caller's per-render timeout at the operator's ceiling
        (round 65) — server-side, like _resolve_politeness, because a render
        holds a host browser seat for as long as it runs."""
        overrides = request.config_overrides
        cap = self._config.politeness.max_request_timeout_seconds
        if overrides is None or overrides.timeout_seconds <= cap:
            return request
        return request.model_copy(
            update={"config_overrides": overrides.model_copy(update={"timeout_seconds": cap})}
        )

    @property
    def _l2_tries_botasaurus(self) -> bool:
        """Whether L2 is configured to attempt Botasaurus before Camoufox
        (fetcher/factory.py::build_level2_fetcher uses the same test)."""
        return "botasaurus" in self._config.levels.level_2.engine

    @property
    def _gateway_fallback_eligible(self) -> bool:
        """Round 49 — whether process_job's circuit-open and
        still-looks-blocked branches may force a level through the paid
        gateway. Gated on strategy=="free_first" specifically (not
        paid_only, which already uses the gateway for every attempt with no
        fallback decision to make, and not free_only, which has no gateway
        to fall back to). A property, not a value cached at __init__ time,
        for the same reason _fetch_with_proxy already re-reads
        self._config.dataimpulse fresh on every call instead of snapshotting
        it once — self._config is mutable for the life of this Worker
        instance. Safe to trust `enabled` alone here without re-checking
        build_gateway_proxy() — __init__'s fail-fast check above already
        guarantees it's configured whenever enabled is True."""
        return (
            self._config.dataimpulse.enabled and self._config.dataimpulse.strategy == "free_first"
        )

    async def _gateway_fallback_usable(self) -> bool:
        """Round 68 — _gateway_fallback_eligible, and the gateway is not
        currently refusing our credentials. A refused gateway makes free_first
        behave exactly like free_only: no forced gateway attempt on an open
        circuit, a pool-blocked domain or a block retry."""
        return self._gateway_fallback_eligible and await self._gateway_health.refusal() is None

    async def _gateway_block_retry_usable(self, result: FetchResult) -> bool:
        """Round 69 — _gateway_fallback_usable() for the block retry, which
        only asks once the pool attempt was blocked. A refusal there marks the
        blocked result, so the URL's terminal failure can say the retry that
        usually gets through was skipped."""
        if await self._gateway_fallback_usable():
            return True
        result.paid_gateway_skipped = True
        return False

    async def process_job(
        self,
        tenant_id: TenantId,
        job_id: str,
        request: ScrapeRequest,
        on_result: Callable[[FetchResult], Awaitable[None]] | None = None,
        deadline: float | None = None,
    ) -> JobStatusResponse:
        """Execute the full escalation state machine for a job.

        on_result (round 29), when given, is awaited once for every URL that
        reaches a terminal outcome (success, a DLQ'd failure, or a cache
        hit) — orchestrator/tasks.py threads this in to persist each result
        to Postgres/S3 as it lands, instead of batching everything until the
        whole job finishes. This is also what makes real per-URL progress
        and mid-job cancellation possible (see _is_cancelled below).

        deadline (round 65) is the time.monotonic() instant rq will kill this
        job at (orchestrator/tasks.py derives it from the rq job). Waits for
        host capacity stop early enough before it that the URL still gets a
        CAPACITY_TIMEOUT row — rq's own kill writes nothing per URL."""
        errors: list[str] = []
        request = self._clamp_timeout(request)
        bypass_cache = bool(request.config_overrides and request.config_overrides.bypass_cache)
        # Round 49 — was a strictly sequential `for url in request.urls:`
        # loop; every URL's full L1->L2->L3 escalation ran to completion
        # before the next one started, which made large batches take far
        # longer than the shared browser/proxy budget actually required
        # (root-caused round 45, deferred until now per an explicit
        # "correctness first" instruction, now in scope). Dispatched
        # concurrently below, bounded by max_concurrent_urls_per_job.
        # PolitenessController and CircuitBreaker are already Redis-atomic
        # per-domain (safe under concurrent callers, including across
        # different jobs, not just within one), and core.budget.
        # BROWSER_SEMAPHORE already caps live browser instances
        # process-wide regardless of how many URL-tasks are in flight — a
        # concurrent task just queues on that semaphore instead of on this
        # one sequential Python loop. `results` is pre-sized and filled by
        # original index so a caller indexing `results[i]` against
        # `request.urls[i]` keeps working even though tasks no longer
        # complete in input order.
        results: list[FetchResult | None] = [None] * len(request.urls)
        cancelled_state = {"value": False}
        semaphore = asyncio.Semaphore(self._config.politeness.max_concurrent_urls_per_job)
        levels = self._resolve_levels(request.config_overrides)
        req_concurrency, req_delay = self._resolve_politeness(request.config_overrides)
        host_cfg = self._config.host_capacity
        admission_deadline_cap = (
            deadline - host_cfg.deadline_margin_seconds if deadline is not None else None
        )

        async def _job_cancelled() -> bool:
            return await self._is_cancelled(tenant_id, job_id)

        already_done = await self._succeeded_urls(tenant_id, job_id)

        async def _dispatch_one_url(index: int, url: HttpUrl) -> None:
            async with semaphore:
                await _process_one_url(index, url)

        async def _process_one_url(index: int, url: HttpUrl) -> None:
            url_str = str(url)
            # Round 63 — per-phase millisecond breakdown for this URL, carried
            # onto whichever FetchResult ends up terminal. duration_ms alone
            # could not distinguish "the fetch is slow" from "the fetch was
            # fine and everything around it was slow", which is exactly the
            # question an external consumer could not answer about a 175s job
            # whose fetch took 28s.
            timings: dict[str, int] = {}
            url_start = time.monotonic()
            # Round 66 — this task's XVFB_LOCK queueing (core/budget.py).
            display_wait = start_display_wait_meter()

            escalations: list[dict[str, Any]] = []

            def _finish(result: FetchResult) -> FetchResult:
                """Stamp the accumulated timings onto a terminal result."""
                timings["total_ms"] = int((time.monotonic() - url_start) * 1000)
                # Like a level that never ran, no wait means no key.
                display_wait_ms = int(display_wait[0] * 1000)
                if display_wait_ms:
                    timings["display_lock_wait_ms"] = display_wait_ms
                result.timings = dict(timings)
                result.escalations = list(escalations) or None
                return result

            def _reject(level: int, result: FetchResult, reason: str) -> None:
                """Record why `level` did not produce this URL's answer (round 64)."""
                entry = {
                    "level": level,
                    "reason": reason,
                    "http_status": result.http_status,
                    "engine": result.engine,
                    "proxy_source": result.proxy_source,
                }
                escalations.append(entry)
                logger.info(
                    "level_rejected job_id=%s url=%s level=%d reason=%s http_status=%s engine=%s",
                    job_id,
                    url_str,
                    level,
                    reason,
                    result.http_status,
                    result.engine,
                )

            # Checked once per task, right after this task's semaphore slot
            # comes free — same cooperative granularity the old "checked
            # before starting the next loop iteration" gave, just per-task
            # instead of per-iteration. The in-memory flag lets every other
            # already-queued task skip its own DB round-trip once any one
            # task has observed cancellation; in-flight tasks that already
            # passed this check are allowed to finish, same as before.
            if cancelled_state["value"]:
                return
            if await self._is_cancelled(tenant_id, job_id):
                cancelled_state["value"] = True
                return
            if url_str in already_done:
                return

            if not bypass_cache:
                cache_start = time.monotonic()
                cached = await self._check_cache(tenant_id, url_str)
                timings["cache_check_ms"] = int((time.monotonic() - cache_start) * 1000)
                if cached is not None:
                    results[index] = _finish(cached)
                    if on_result is not None:
                        await on_result(cached)
                    return

            domain = self._extract_domain(url_str)
            admission: _RenderAdmission | None = None
            if self._admission is not None:
                url_deadline = time.monotonic() + host_cfg.per_url_admission_cap_seconds
                if admission_deadline_cap is not None:
                    url_deadline = min(url_deadline, admission_deadline_cap)
                admission = _RenderAdmission(
                    tenant_id=tenant_id,
                    domain=domain,
                    concurrency=req_concurrency,
                    delay_seconds=req_delay,
                    priority_ms=int(time.time() * 1000),
                    deadline=url_deadline,
                    is_cancelled=_job_cancelled,
                    timings=timings,
                )
            plan = await self._level_memory.plan(tenant_id, domain, levels)
            start_level = plan.start_level
            # Round 64 — a domain known to refuse the free pool goes straight
            # to the gateway (see LevelMemory.plan). Only where the gateway
            # fallback exists at all: under free_only there is nowhere to go.
            # Round 68 — nor while the gateway is refusing our credentials:
            # the pool may be blocked here, but it is the only path that can
            # still produce a real outcome for this URL.
            gateway_first = plan.skip_pool and await self._gateway_fallback_usable()
            # Round 69 — the gateway was wanted on this URL's path but refused
            # (here, or at the circuit/block-retry points below, or inside
            # _fetch_with_proxy). A failure then says so, instead of reading as
            # "this site blocks scrapers" when the usual route was down.
            gateway_skipped = (
                plan.skip_pool and self._gateway_fallback_eligible and not gateway_first
            )
            skip_botasaurus = plan.skip_botasaurus
            if gateway_first:
                logger.info(
                    "pool_skipped_known_block job_id=%s url=%s domain=%s",
                    job_id,
                    url_str,
                    domain,
                )
            # An explicit min_level is the caller's own floor and already
            # narrowed `levels`, so the hint can only move the start UP from
            # there — never below what the caller asked for.
            # Which levels actually ran is readable from the timings dict
            # itself: a skipped level simply has no level_N_ms key.
            url_levels = [level for level in levels if level >= start_level]
            # Round 42 — wraps the rest of this URL's fetch/extract/markdown
            # pipeline in a try/except so an unexpected exception ANYWHERE in
            # it (fetch dispatch, extraction, markdown conversion, DLQ/on_result
            # bookkeeping) degrades to a single failed result for THIS url,
            # never aborts the whole job and abandons every other URL still
            # queued. Live-caught: a RecursionError inside markdownify()
            # against one real, deeply-nested article page propagated all the
            # way up through this method, out through orchestrator/tasks.py's
            # _run_scrape_job, and crashed the entire job process — 4 URLs had
            # already genuinely succeeded (and stayed persisted, since
            # on_result already streamed them — round 29), but the remaining
            # 47 in that batch were never even attempted. asyncio.CancelledError
            # and KeyboardInterrupt are BaseException, not Exception, so
            # mid-job cancellation (_is_cancelled above) and process shutdown
            # keep propagating through this unaffected. `domain` is computed
            # above, outside the try, since it's needed in the except handler
            # too and _extract_domain() is a pure, effectively infallible
            # urlparse call on an already-pydantic-validated HttpUrl.
            try:
                # Round 42 — tracks the most recent real FetchResult seen across
                # the level loop below, so the for/else terminal branch can
                # report the REAL last failure instead of fabricating one. See
                # that branch's comment for the bug this closes.
                last_level_result: FetchResult | None = None

                for level in url_levels:
                    circuit_open = not await self._circuit_breaker.allow_request(domain)
                    # Round 49 — an open circuit reflects FREE-pool failure
                    # history for this domain (record_success/record_failure
                    # below fire regardless of proxy source, but under
                    # free_only/paid_only every attempt IS the free pool or
                    # the gateway respectively, so historically "circuit
                    # open" and "free pool failing" were the same thing).
                    # Under free_first specifically, that conflation is
                    # wrong: the gateway is a structurally different network
                    # path a domain's free-proxy-driven circuit trip says
                    # nothing about, and it's available right now, not after
                    # a cooldown. free_only/paid_only/gateway-not-configured
                    # keep the exact prior behavior: immediate CIRCUIT_OPEN
                    # DLQ, no attempt made. Round 68 — so does free_first
                    # while the gateway is refusing our credentials.
                    if circuit_open and not await self._gateway_fallback_usable():
                        if self._gateway_fallback_eligible:
                            gateway_skipped = True
                        circuit_result = FetchResult(
                            url=url_str,
                            success=False,
                            level_used=level,
                            duration_ms=0,
                            failure_category=FailureCategory.CIRCUIT_OPEN,
                            error_message=f"Circuit open for {domain}",
                        )
                        _note_gateway_skipped(circuit_result, gateway_skipped)
                        await self._dlq.enqueue(
                            tenant_id,
                            job_id,
                            url_str,
                            FailureCategory.CIRCUIT_OPEN,
                            circuit_result.error_message or "",
                            level,
                        )
                        errors.append(circuit_result.error_message or "")
                        results[index] = _finish(circuit_result)
                        if on_result is not None:
                            await on_result(circuit_result)
                        break
                    # Level 1 never leases a proxy through _fetch_with_proxy
                    # at all (see _fetch_url docstring) — there's no gateway
                    # path to force it through. Under free_first with the
                    # circuit open, skip straight to level 2 (where the
                    # gateway attempt below CAN apply) instead of either
                    # DLQ'ing outright or pretending L1 has a gateway mode.
                    if circuit_open and level == 1:
                        continue

                    # Round 65 — with host admission on, a browser level takes
                    # its politeness slot and delay inside the per-render claim
                    # in _fetch_with_proxy, together with the browser seat.
                    # Taking the slot out here first is what let one URL sit on
                    # it through an untimed browser wait and starve its
                    # same-site siblings into the 300s slot timeout.
                    claims_per_render = admission is not None and level >= 2
                    held: contextlib.AbstractAsyncContextManager[None] = contextlib.nullcontext()
                    if not claims_per_render:
                        # Round 63 — the inter-fetch delay is served BEFORE taking
                        # a slot, not while holding one. Sleeping inside the slot
                        # made every waiter pay for the delay too: with
                        # default_concurrency slots and a per-fetch delay, the
                        # pool's real throughput was one URL per (delay + fetch),
                        # not one per fetch. It is also the correct order on its
                        # own terms — the delay is about the TARGET's pacing, the
                        # slot is about our own concurrency.
                        timings["politeness_wait_ms"] = timings.get(
                            "politeness_wait_ms", 0
                        ) + await self._politeness.wait_if_needed(
                            domain, tenant_id, delay_seconds=req_delay
                        )
                        slot_worker_id, slot_wait_ms = await self._acquire_politeness_slot(
                            domain, tenant_id, concurrency=req_concurrency
                        )
                        timings["slot_wait_ms"] = timings.get("slot_wait_ms", 0) + slot_wait_ms
                        if slot_worker_id is None:
                            # Round 63 — this used to `continue` to the NEXT level.
                            # A busy slot says nothing about the current level, so
                            # advancing on it let a URL walk the whole ladder
                            # without a single fetch and then DLQ as if the proxy
                            # pool were exhausted. Contention is terminal for this
                            # URL now, reported as what it actually is.
                            message = (
                                f"No politeness slot for {domain} within "
                                f"{self._config.politeness.slot_wait_timeout_seconds}s"
                            )
                            slot_result = FetchResult(
                                url=url_str,
                                success=False,
                                level_used=level,
                                duration_ms=0,
                                failure_category=FailureCategory.POLITENESS_TIMEOUT,
                                error_message=message,
                            )
                            await self._dlq.enqueue(
                                tenant_id,
                                job_id,
                                url_str,
                                FailureCategory.POLITENESS_TIMEOUT,
                                message,
                                level,
                            )
                            errors.append(message)
                            results[index] = _finish(slot_result)
                            if on_result is not None:
                                await on_result(slot_result)
                            break

                        held = self._politeness.held_slot(domain, tenant_id, slot_worker_id)

                    level_start = time.monotonic()
                    waited_before = timings.get("admission_wait_ms", 0)
                    async with held:
                        result = await self._fetch_url(
                            tenant_id,
                            url_str,
                            level,
                            request.config_overrides,
                            force_gateway=circuit_open or gateway_first,
                            skip_botasaurus=skip_botasaurus,
                            admission=admission if claims_per_render else None,
                        )
                    # level_N_ms is render time only; queueing for host
                    # capacity is reported separately as admission_wait_ms.
                    level_ms = int((time.monotonic() - level_start) * 1000) - (
                        timings.get("admission_wait_ms", 0) - waited_before
                    )
                    timings[f"level_{level}_ms"] = level_ms
                    fetch_duration_seconds.labels(level=str(level)).observe(level_ms / 1000)

                    if result is None:
                        continue

                    last_level_result = result
                    if result.paid_gateway_skipped:
                        gateway_skipped = True

                    # Round 49 — one gateway retry before conceding a
                    # final-level block, evaluated BEFORE branching on
                    # result.success so it covers both real shapes a block
                    # takes: a fetcher-level failure (success=False,
                    # failure_category=DETECTION_BLOCK — a definitive
                    # 401/403/404/405/410/429, see fetcher/_failure.py's
                    # classify_http_status) and a success=True result whose
                    # CONTENT still looks like a challenge page (round 45's
                    # original gap; is_challenge_page's own first check is
                    # also status-code-based, so this second clause also
                    # catches a browser-reported "success" carrying a block
                    # status). Missing the first shape was a real gap live-
                    # verifying this round against a real crunchbase.com
                    # 403 — that domain fails as a clean fetcher-level
                    # DETECTION_BLOCK, never as a success=True challenge
                    # page, so the original (success-branch-only) version
                    # of this retry never fired for it at all. Never on a
                    # result that already came from the gateway (one extra
                    # attempt per URL per level, never a second).
                    #
                    # Round 64 — at EVERY level, not only the final one. The
                    # old reasoning ("retrying a non-final level's block is
                    # pointless — it's about to escalate anyway") treated
                    # every block as the LEVEL's fault. Live, it was the
                    # PROXY's: Jumia 403s free-pool (datacenter) exits at L2,
                    # while the same L2 through the gateway returned 200 with
                    # ~600 links in 20-25s. Escalating instead paid a free-pool
                    # L3 attempt (blocked the same way) and then this same
                    # retry at L3 anyway — and taught level memory that the
                    # domain needs L3, so every later URL skipped L2 too.
                    if (
                        self._gateway_fallback_eligible
                        and result.proxy_source != "paid_gateway"
                        and (
                            result.failure_category == FailureCategory.DETECTION_BLOCK
                            or (
                                result.success
                                and self._challenge_detector.is_challenge_page(
                                    result.html or "",
                                    result.http_status or 200,
                                    short_page_is_suspect=False,
                                )
                            )
                        )
                        # Round 68 — checked last: it is the only clause that
                        # reads Redis, and only a blocked result gets here.
                        # Round 69 — a refusal here is remembered for the
                        # terminal result (_gateway_block_retry_usable).
                        and await self._gateway_block_retry_usable(result)
                    ):
                        # Round 64 — the attempt being retried is a rejection
                        # like any other, and the retry is a phase like any
                        # other. Neither was recorded: a live L2 probe showed
                        # total_ms ~2x level_2_ms with nothing to account for
                        # the difference, and a result whose only visible
                        # trace was `proxy_source: paid_gateway`.
                        _reject(
                            level,
                            result,
                            f"failure:{result.failure_category.value}"
                            if not result.success and result.failure_category is not None
                            else self._challenge_detector.challenge_reason(
                                result.html or "",
                                result.http_status or 200,
                                short_page_is_suspect=False,
                            )
                            or "blocked",
                        )
                        retry_start = time.monotonic()
                        gateway_result = await self._fetch_url(
                            tenant_id,
                            url_str,
                            level,
                            request.config_overrides,
                            force_gateway=True,
                            skip_botasaurus=skip_botasaurus,
                            admission=admission if claims_per_render else None,
                        )
                        timings[f"level_{level}_gateway_retry_ms"] = int(
                            (time.monotonic() - retry_start) * 1000
                        )
                        if gateway_result is not None:
                            result = gateway_result
                            last_level_result = result
                            if (
                                gateway_result.proxy_source == "paid_gateway"
                                and gateway_result.success
                                and not self._looks_blocked(gateway_result)
                            ):
                                # The pool was blocked, the gateway at the
                                # same level got real content: the domain
                                # refuses the pool, not this level.
                                #
                                # Round 68 — only on a real gateway success.
                                # "Not blocked" alone also held for a 407: with
                                # the plan out of traffic every refused retry
                                # taught level memory the domain refuses the
                                # pool, and each later URL of it went
                                # gateway-first into the same refusal (10
                                # domains live, research_agent). The retry may
                                # also have come back from the pool, once
                                # _fetch_with_proxy fell back on a refusal.
                                await self._level_memory.record_pool_blocked(tenant_id, domain)

                    # Round 69 — a refused block retry, or a gateway retry that
                    # _fetch_with_proxy served from the pool instead.
                    if result.paid_gateway_skipped:
                        gateway_skipped = True

                    if result.success:
                        await self._circuit_breaker.record_success(domain)
                        # `FetchResult.is_challenge_page` was declared on the model,
                        # persisted, and even gated dedup.py's caching decision, but
                        # no fetcher ever actually set it — L1 in particular only
                        # checks the HTTP status code (`success = status < 400`), so
                        # a 200 response whose body is literally an unsolved
                        # challenge/interstitial page (e.g. a JS proof-of-work gate)
                        # was accepted as real content and never escalated. Classify
                        # it here, once, centrally — same rationale as the
                        # extraction/markdown wiring below — so every level's result
                        # is labeled correctly regardless of which fetcher produced
                        # it. short_page_is_suspect=False matches the convention
                        # L2/L3's own internal solve-polling loops already use, so a
                        # short-but-genuinely-solved page isn't misclassified.
                        result.is_challenge_page = self._challenge_detector.is_challenge_page(
                            result.html or "",
                            result.http_status or 200,
                            short_page_is_suspect=False,
                        )
                        # A JS-gated shell or an unsolved challenge page from a
                        # non-final level is not real content — an HTTP-only L1
                        # fetch of a SPA returns 200 with an empty mount point, and
                        # an HTTP-only L1 fetch of a JS PoW challenge returns 200
                        # with the interstitial itself. Escalate to a browser level
                        # instead of caching either as success (round 15 for the
                        # JS-gated-shell half of this; this round for the
                        # challenge-page half). Browser levels render JS and already
                        # loop internally until solved or exhausted, so a genuine L2/
                        # L3 success usually won't trip this.
                        # Round 69 — a block status the challenge detector
                        # does not list (401, 405, 410) is a block too:
                        # fetcher/_failure.py already classes all six as
                        # DETECTION_BLOCK. Live, a 401 at L3 was stored as a
                        # success whose "content" was the page title.
                        # Checked here, not in CHALLENGE_STATUS_CODES, which
                        # also makes L2/L3 wait for a solve that never comes.
                        blocked_status = (
                            classify_http_status(result.http_status or 200)
                            == FailureCategory.DETECTION_BLOCK
                        )
                        still_looks_blocked = (
                            result.is_challenge_page
                            or blocked_status
                            or self._challenge_detector.looks_javascript_gated(result.html or "")
                        )
                        if still_looks_blocked:
                            block_reason = self._challenge_detector.challenge_reason(
                                result.html or "",
                                result.http_status or 200,
                                short_page_is_suspect=False,
                            ) or (f"status:{result.http_status}" if blocked_status else "js_gated")
                            _reject(level, result, block_reason)
                        if level < url_levels[-1] and still_looks_blocked:
                            continue
                        # Round 45 — the final level used to unconditionally accept
                        # "whatever it got," even a page that STILL looks blocked
                        # after a real, JS-capable browser rendered it. Live-caught:
                        # a genuine 404 error page (businessday.ng) was silently
                        # persisted as 6KB of "successful" markdown for days,
                        # because L3 had nowhere further to escalate to and so
                        # accepted it as-is. A page that's still blocked/not-found
                        # after the most capable fetcher's own real render is
                        # actual evidence of a real problem — downgrade to a real
                        # failure (mutating `result` in place, which
                        # `last_level_result` already points at) and `continue`,
                        # the same as any other per-level failure. Since this is
                        # necessarily the final level (the non-final case already
                        # `continue`d above), the for loop simply ends here,
                        # reusing the EXISTING for/else "all levels exhausted"
                        # fallback below to construct the real DLQ entry from
                        # `last_level_result` — not duplicating that logic here.
                        if still_looks_blocked:
                            await self._circuit_breaker.record_failure(domain)
                            result.success = False
                            result.failure_category = (
                                classify_http_status(result.http_status or 0)
                                or FailureCategory.DETECTION_BLOCK
                            )
                            result.block_reason = block_reason
                            result.error_message = result.error_message or (
                                "still blocked/not-found after final level"
                            )
                            continue
                        # Round 63 — this is the real "this level produced
                        # usable content" point, past both still_looks_blocked
                        # gates, so it is the only honest place to teach the
                        # level memory. Recording at `if result.success` above
                        # would have taught a level whose "success" is about to
                        # be reclassified as a block.
                        await self._level_memory.record_success(tenant_id, domain, level)
                        if result.proxy_source == "pool":
                            await self._level_memory.record_pool_ok(tenant_id, domain)
                        if level == 2 and self._l2_tries_botasaurus and not skip_botasaurus:
                            if result.engine == "botasaurus":
                                await self._level_memory.record_botasaurus_ok(tenant_id, domain)
                            elif result.engine == "camoufox":
                                await self._level_memory.record_botasaurus_failed(tenant_id, domain)
                        if result.html:
                            extract_start = time.monotonic()
                            # FetchResult.extracted was declared on the model and
                            # persisted by orchestrator/tasks.py, but nothing ever
                            # populated it — AdaptiveSelector existed, fully
                            # tested, with zero callers (round 28). Wired here,
                            # once, so it applies uniformly regardless of which
                            # level actually succeeded.
                            from scraper_engine.fetcher.adaptive_selector import AdaptiveSelector

                            schema = (
                                request.config_overrides.extraction_schema
                                if request.config_overrides
                                else None
                            )
                            # extraction-engine is used only when both a real schema was
                            # supplied AND EXTRACTION_ENGINE_BASE_URL is configured;
                            # otherwise (and on any extraction-engine failure — it fails
                            # soft, returning None rather than raising) this falls back
                            # to today's exact AdaptiveSelector behavior unchanged.
                            extracted = None
                            if self._extraction_engine is not None and schema:
                                extracted = await self._extraction_engine.extract(
                                    result.html,
                                    schema,
                                    enable_smallmodel=(
                                        request.config_overrides.extraction_enable_smallmodel
                                        if request.config_overrides
                                        else False
                                    ),
                                    enable_llm=(
                                        request.config_overrides.extraction_enable_llm
                                        if request.config_overrides
                                        else False
                                    ),
                                )
                            if extracted is None:
                                # base_url (round 63): links come back
                                # absolute, so a caller can follow them
                                # without re-deriving the page's origin.
                                extracted = await AdaptiveSelector(
                                    max_links=self._config.extraction.max_links
                                ).extract(result.html, schema=schema, base_url=url_str)
                            result.extracted = extracted
                            timings["extract_ms"] = int((time.monotonic() - extract_start) * 1000)
                            markdown_start = time.monotonic()
                            # Markdown conversion (round 29) — same "wired once,
                            # applies regardless of level" rationale as
                            # extraction above. Previously only L1 ever produced
                            # markdown (inline Firecrawl calls in
                            # fetcher/level_1.py); centralizing here means a
                            # page that had to escalate to L2/L3 still gets
                            # clean markdown, not just raw HTML. markdown is its
                            # own field on FetchResult, independent of
                            # `extracted` — a caller who only wants the markdown
                            # (e.g. to hand to their own extraction model) can
                            # just read that field and ignore `extracted`.
                            if self._firecrawl is not None:
                                result.markdown = await self._firecrawl.convert_to_markdown(
                                    result.html, url_str
                                )
                            else:
                                # Firecrawl is opt-in (FIRECRAWL_API_KEY/
                                # FIRECRAWL_BASE_URL) — without it, markdown used
                                # to be left None entirely, so a caller with no
                                # Firecrawl instance only ever got `extracted`
                                # (title/body/links), not markdown. Converting
                                # the HTML already in hand locally (round 33)
                                # means markdown is populated unconditionally.
                                from scraper_engine.services.markdown_fallback import (
                                    html_to_markdown,
                                )

                                result.markdown = html_to_markdown(result.html)
                            timings["markdown_ms"] = int((time.monotonic() - markdown_start) * 1000)
                        results[index] = _finish(result)
                        if on_result is not None:
                            await on_result(result)
                        break
                    else:
                        # Round 66 — a proxy refusing our credentials says
                        # nothing about the domain. From the gateway it is
                        # also terminal for this URL: every later level and
                        # retry would go out through the same refused
                        # account. The DLQ reaper re-drives it once a probe
                        # through the gateway succeeds again.
                        #
                        # Round 68 — only paid_only still gets here with a
                        # gateway refusal. Under free_first the gateway is a
                        # fallback, and _fetch_with_proxy retries a refused
                        # attempt on the free pool before it returns.
                        category = result.failure_category
                        auth_failed = category == FailureCategory.PROXY_AUTH_FAILED
                        gateway_refused = auth_failed and result.proxy_source == "paid_gateway"
                        if not auth_failed:
                            await self._circuit_breaker.record_failure(domain)
                        _reject(
                            level,
                            result,
                            f"failure:{category.value}"
                            if category is not None
                            else "failure:unknown",
                        )
                        if category is not None and (
                            category in DLQ_ELIGIBLE_CATEGORIES or gateway_refused
                        ):
                            _note_gateway_skipped(result, gateway_skipped)
                            await self._dlq.enqueue(
                                tenant_id,
                                job_id,
                                url_str,
                                category,
                                result.error_message or "",
                                level,
                            )
                            errors.append(result.error_message or "DLQ")
                            results[index] = _finish(result)
                            if on_result is not None:
                                await on_result(result)
                            break
                else:
                    # Round 42 — this branch used to hardcode
                    # FailureCategory.PROXY_EXHAUSTED/"All fetch levels
                    # exhausted" here regardless of why every level actually
                    # failed. Live-caught: under dataimpulse.strategy=paid_only,
                    # ProxyManager.get_proxy() is never even called (see
                    # _fetch_with_proxy's paid_only branch) — real proxy-pool
                    # exhaustion is structurally impossible — yet DLQ entries
                    # still showed failure_category=proxy_exhausted,
                    # error_message="All fetch levels exhausted" for every URL
                    # that failed all 3 levels for ANY reason (a browser crash,
                    # a network timeout, a detection block, anything not in
                    # DLQ_ELIGIBLE_CATEGORIES, since those categories are
                    # designed to fall through and escalate rather than break
                    # early). That destroyed the real diagnostic signal and fed
                    # proxy/dlq_reaper.py's PROXY_EXHAUSTED-specific auto-retry
                    # gate (pool-health-based) an entry whose real cause often
                    # had nothing to do with proxy pool health at all. Now uses
                    # the real last attempt's category/message, tracked via
                    # last_level_result above — falls back to the historical
                    # label only in the one genuinely-unattempted case.
                    #
                    # Round 63 — the case that fallback was written for
                    # (politeness contention starving every level) no longer
                    # reaches here: slot contention is terminal at the level
                    # it happens on and reports POLITENESS_TIMEOUT. What can
                    # still land here unattempted is a circuit that was open
                    # at L1 under free_first and then closed-but-unavailable
                    # for the rest of the ladder.
                    if last_level_result is not None:
                        real_category = last_level_result.failure_category or (
                            FailureCategory.PROXY_EXHAUSTED
                        )
                        real_message = (
                            last_level_result.error_message or "All fetch levels exhausted"
                        )
                    else:
                        real_category = FailureCategory.PROXY_EXHAUSTED
                        real_message = "All fetch levels exhausted without a single attempt"
                    exhausted_result = FetchResult(
                        url=url_str,
                        success=False,
                        level_used=url_levels[-1],
                        duration_ms=0,
                        failure_category=real_category,
                        error_message=real_message,
                        # Round 49 — carried forward so a DLQ'd/exhausted
                        # result stays traceable to whether the last real
                        # attempt went through the free pool or the paid
                        # gateway. Round 69 — http_status and
                        # is_challenge_page too: this branch dropped them, so
                        # every terminal block was stored with a NULL status
                        # and only its message said "http_status=403".
                        proxy_source=(
                            last_level_result.proxy_source if last_level_result else None
                        ),
                        http_status=(last_level_result.http_status if last_level_result else None),
                        is_challenge_page=(
                            last_level_result.is_challenge_page if last_level_result else False
                        ),
                    )
                    if (
                        last_level_result is not None
                        and real_category == FailureCategory.DETECTION_BLOCK
                    ):
                        _describe_block(exhausted_result, last_level_result)
                    _note_gateway_skipped(exhausted_result, gateway_skipped)
                    real_message = exhausted_result.error_message or real_message
                    await self._dlq.enqueue(
                        tenant_id,
                        job_id,
                        url_str,
                        real_category,
                        real_message,
                        url_levels[-1],
                    )
                    errors.append(real_message)
                    results[index] = _finish(exhausted_result)
                    if on_result is not None:
                        await on_result(exhausted_result)
            except AdmissionCancelledError:
                # The job was cancelled while this URL waited for capacity:
                # same outcome as noticing it before starting (no result).
                cancelled_state["value"] = True
            except (AdmissionTimeoutError, AdmissionUnavailableError) as exc:
                # Round 65 — our own capacity or our own Redis, never the
                # target: no circuit_breaker.record_failure and no level
                # memory, mirroring the POLITENESS_TIMEOUT branch above.
                category = (
                    FailureCategory.CAPACITY_TIMEOUT
                    if isinstance(exc, AdmissionTimeoutError)
                    else FailureCategory.DEPENDENCY_UNAVAILABLE
                )
                if isinstance(exc, AdmissionTimeoutError):
                    timings["admission_wait_ms"] = (
                        timings.get("admission_wait_ms", 0) + exc.waited_ms
                    )
                message = str(exc)
                capacity_result = FetchResult(
                    url=url_str,
                    success=False,
                    level_used=url_levels[-1],
                    duration_ms=0,
                    failure_category=category,
                    error_message=message,
                )
                await self._dlq.enqueue(
                    tenant_id, job_id, url_str, category, message, url_levels[-1]
                )
                errors.append(message)
                results[index] = _finish(capacity_result)
                if on_result is not None:
                    await on_result(capacity_result)
            except Exception as exc:
                logger.exception(
                    "process_job_unexpected_url_failure job_id=%s url=%s",
                    job_id,
                    url_str,
                )
                # Round 65 — our own Redis failing anywhere on this path
                # (circuit breaker, level memory, proxy manager, politeness —
                # not only host admission) is ours, not the target's: live, a
                # 20s Redis pause mislabelled 4 URLs PARSE_ERROR and counted
                # them against the domain's circuit.
                redis_down = isinstance(exc, RedisConnectionError | RedisTimeoutError)
                category = (
                    FailureCategory.DEPENDENCY_UNAVAILABLE
                    if redis_down
                    else FailureCategory.PARSE_ERROR
                )
                if not redis_down:
                    await self._circuit_breaker.record_failure(domain)
                crash_result = FetchResult(
                    url=url_str,
                    success=False,
                    level_used=url_levels[-1],
                    duration_ms=0,
                    failure_category=category,
                    error_message=f"Unexpected error processing URL: {exc}",
                )
                await self._dlq.enqueue(
                    tenant_id,
                    job_id,
                    url_str,
                    category,
                    crash_result.error_message or "",
                    url_levels[-1],
                )
                errors.append(crash_result.error_message or "Unexpected error")
                results[index] = _finish(crash_result)
                if on_result is not None:
                    await on_result(crash_result)

        await asyncio.gather(*(_dispatch_one_url(i, u) for i, u in enumerate(request.urls)))

        cancelled = cancelled_state["value"]
        # None entries are URLs a task returned from early on (cancellation
        # observed before any real work started) — never appended to
        # `errors` either, so filtering them out here keeps `results` and
        # `errors` consistent with what actually ran, same as the old
        # sequential loop's `break`-before-append behavior.
        final_results = [r for r in results if r is not None]
        any_success = any(r.success for r in final_results)
        status = (
            JobStatus.CANCELLED
            if cancelled
            else (
                JobStatus.COMPLETED
                if not errors
                else (JobStatus.FAILED if not any_success else JobStatus.COMPLETED)
            )
        )
        # partial_failure: status says COMPLETED but it isn't a clean run —
        # some URLs DLQ'd while others succeeded. Kept as a boolean flag
        # rather than a new JobStatus value (see JobStatusResponse docstring)
        # so a caller polling status can't mistake this for full success.
        partial_failure = bool(errors) and any_success
        return JobStatusResponse(
            job_id=job_id,
            status=status,
            progress=1.0,
            results=final_results if final_results else None,
            error="; ".join(errors) if errors else None,
            partial_failure=partial_failure,
        )

    async def _is_cancelled(self, tenant_id: TenantId, job_id: str) -> bool:
        """Cooperative mid-loop cancellation check (round 29) — a cheap point
        read before starting work on the next URL. Worst-case cancellation
        latency is however long the in-flight URL's own L1->L2->L3 escalation
        takes, matching the granularity on_result already persists at."""
        if self._pg is None:
            return False
        row = await self._pg.fetchrow(
            tenant_id,
            "SELECT status FROM scrape_jobs WHERE job_id = $1::uuid",
            job_id,
        )
        return row is not None and row["status"] == JobStatus.CANCELLED.value

    async def _succeeded_urls(self, tenant_id: TenantId, job_id: str) -> set[str]:
        """URLs this same job already scraped successfully (round 65).

        A job runs again under its own id when proxy/dlq_reaper.py re-drives
        one of its DLQ'd URLs, and that re-run walks the whole URL list. The
        reuse cache used to be what kept the finished URLs from being fetched
        again — but a caller who asked for bypass_cache (the consumer this
        round's work came from does, on every job) got every one of them
        re-rendered, on a host the re-drive was meant to spare. Their results
        are already persisted, so they are skipped outright.
        """
        if self._pg is None:
            return set()
        rows = await self._pg.fetch(
            tenant_id,
            "SELECT DISTINCT url FROM scrape_results WHERE job_id = $1::uuid AND success = true",
            job_id,
        )
        return {row["url"] for row in rows}

    async def _check_cache(self, tenant_id: TenantId, url: str) -> FetchResult | None:
        """Reuse a recent successful scrape of this exact URL for this tenant
        instead of re-fetching (round 29). Sliding TTL — see CACHE_TTL_DAYS.
        Deliberately does not restore `html` (no need to re-store an S3
        snapshot on a cache hit — orchestrator/tasks.py's persist step just
        carries the existing html_snapshot_url pointer forward)."""
        if self._pg is None:
            return None
        row = await self._pg.fetchrow(
            tenant_id,
            f"""
            SELECT http_status, is_challenge_page, level_used, proxy_used,
                   markdown, json_data, html_snapshot_url
            FROM scrape_results
            WHERE url = $1 AND success = true
              AND extracted_at > NOW() - INTERVAL '{CACHE_TTL_DAYS} days'
              -- Round 69: a "success" stored with a block status (a 401 at
              -- L3, before process_job treated it as blocked) is not content.
              AND COALESCE(http_status, 200) < 400
            ORDER BY extracted_at DESC
            LIMIT 1
            """,
            url,
        )
        if row is None:
            return None
        return FetchResult(
            url=url,
            success=True,
            http_status=row["http_status"],
            is_challenge_page=row["is_challenge_page"],
            level_used=row["level_used"],
            proxy_used=row["proxy_used"],
            markdown=row["markdown"],
            extracted=json.loads(row["json_data"]) if row["json_data"] else None,
            html_snapshot_url=row["html_snapshot_url"],
            from_cache=True,
            duration_ms=0,
        )

    async def _fetch_url(
        self,
        tenant_id: TenantId,
        url: str,
        level: int,
        overrides: ConfigOverrides | None = None,
        force_gateway: bool = False,
        skip_botasaurus: bool = False,
        admission: _RenderAdmission | None = None,
    ) -> FetchResult | None:
        """Dispatch fetch to the appropriate level fetcher.

        force_gateway (round 49): only meaningful for level 2/3 — level 1
        never leases a proxy through _fetch_with_proxy at all (L1 is
        HTTP-only and doesn't participate in the free/paid proxy decision),
        so it's accepted here but ignored for level == 1."""
        if level == 1:
            from scraper_engine.fetcher.factory import build_level1_fetcher

            l1_fetcher = build_level1_fetcher(self._config)
            return await l1_fetcher.fetch(url, tenant_id, overrides=overrides)
        elif level == 2:
            from scraper_engine.core.exceptions import PostgresClientMissingError
            from scraper_engine.fetcher.factory import build_level2_fetcher

            if self._pg is None:
                raise PostgresClientMissingError(level=level)

            def _build_l2() -> object:
                return build_level2_fetcher(
                    self._config,
                    captcha_solver=self._captcha_solver,
                    pool=self._browser_pool,
                    botasaurus_pool=self._botasaurus_pool,
                    skip_botasaurus=skip_botasaurus,
                )

            host_cfg = self._config.host_capacity
            weight = host_cfg.camoufox_weight
            if self._l2_tries_botasaurus and not skip_botasaurus:
                # Botasaurus first, Camoufox as fallback, sequentially inside
                # one render claim: weigh it as the heavier of the two.
                weight = max(weight, host_cfg.botasaurus_weight)
            return await self._fetch_with_proxy(
                tenant_id,
                url,
                level,
                overrides,
                self._pg,
                _build_l2,
                force_gateway=force_gateway,
                admission=admission,
                weight=weight,
            )
        elif level == 3:
            from scraper_engine.core.exceptions import PostgresClientMissingError
            from scraper_engine.fetcher.factory import build_level3_fetcher

            if self._pg is None:
                raise PostgresClientMissingError(level=level)

            def _build_l3() -> object:
                return build_level3_fetcher(
                    self._config,
                    captcha_solver=self._captcha_solver,
                    pool=self._browser_pool,
                )

            return await self._fetch_with_proxy(
                tenant_id,
                url,
                level,
                overrides,
                self._pg,
                _build_l3,
                force_gateway=force_gateway,
                admission=admission,
                weight=self._config.host_capacity.camoufox_weight,
            )
        return None

    async def _fetch_with_proxy(
        self,
        tenant_id: TenantId,
        url: str,
        level: int,
        overrides: ConfigOverrides | None,
        pg: PostgresClient,
        build_fetcher: Callable[[], Any],
        force_gateway: bool = False,
        admission: _RenderAdmission | None = None,
        weight: float = 1.0,
    ) -> FetchResult:
        """Shared L2/L3 lease-fetch-score cycle, with a bounded same-level
        retry (round 37, see _PROXY_RETRYABLE_CATEGORIES/
        _SAME_LEVEL_PROXY_RETRIES above) when the real fetch fails for a
        reason the lease-time preflight can't predict but is still
        plausibly proxy-caused. mark_success/mark_failure wiring is round
        32's; retrying on a fresh lease after mark_failure is round 37's.
        `pg` is passed explicitly (not read from self._pg) so the caller's
        `if self._pg is None: raise` guard narrows it to non-None across
        the function boundary — mypy can't carry that narrowing through a
        separate method call on `self._pg` directly.

        force_gateway (round 49): used by process_job's circuit-open and
        still-looks-blocked gateway-fallback branches to force this one
        attempt through the paid gateway regardless of `strategy` — the
        caller has already decided a free-pool attempt isn't appropriate
        (circuit open) or didn't work (still blocked after a real render),
        not something this method re-derives.

        admission (round 65): when given, every pass of the loop below — the
        first render, each pool retry, each gateway rotation — claims host
        capacity first (_render_claim), BEFORE leasing a proxy, so a proxy is
        never held idle in the host's line."""
        from scraper_engine.core.exceptions import ProxyPoolExhaustedError
        from scraper_engine.proxy.lease import ProxyLease
        from scraper_engine.proxy.manager import ProxyManager

        # Round 40 — three-way toggle (config/schema.py::DataImpulseConfig).
        # free_only is the default and is byte-for-byte the pre-round-40 code
        # path below (pm.get_proxy() every attempt, no gateway involved).
        di_cfg = self._config.dataimpulse
        strategy = di_cfg.strategy if di_cfg.enabled else "free_only"

        pm = ProxyManager(redis=self._redis, pg=pg, tier_config=self._config.proxy_tiers)
        domain = self._extract_domain(url)

        # Round 62 — two independent retry budgets, deliberately not merged
        # into one counter. pool_retries_left is round 37's: a fresh lease
        # from the scored free pool after a plausibly-proxy-caused crash or
        # timeout. block_rotations_left is new: a fresh GATEWAY EXIT IP
        # after the target blocked us on reputation. They fire on disjoint
        # categories and on different proxy sources, so one shared budget
        # would let a crash loop eat the rotation allowance (or vice versa)
        # and silently leave the real failure unretried.
        pool_retries_left = _SAME_LEVEL_PROXY_RETRIES
        block_rotations_left = di_cfg.rotate_on_block_retries if di_cfg.enabled else 0
        # Round 68 — the gateway is refusing our credentials (shared verdict,
        # proxy/gateway_health.py, or a 407 seen by this very call). Under
        # free_first the pool is then the only path; under paid_only there is
        # no path at all, and the URL fails without a render.
        gateway_refused = False
        # Round 69 — this call wanted the gateway (forced, or as the
        # exhausted pool's fallback) and went without it; stamped on the
        # result as `paid_gateway_skipped`.
        gateway_bypassed = False

        while True:
            if (force_gateway or strategy == "paid_only") and not gateway_refused:
                gateway_refused = await self._gateway_health.refusal() is not None
            if gateway_refused and strategy == "paid_only":
                return FetchResult(
                    url=url,
                    success=False,
                    level_used=level,
                    duration_ms=0,
                    failure_category=FailureCategory.PROXY_AUTH_FAILED,
                    error_message=_GATEWAY_REFUSED_MESSAGE,
                    proxy_source="paid_gateway",
                    paid_gateway_skipped=True,
                )
            if force_gateway and gateway_refused:
                gateway_bypassed = True
            async with self._render_claim(admission, weight):
                lease: ProxyLease
                if force_gateway and not gateway_refused:
                    gateway_proxy = self._new_gateway_proxy()
                    if gateway_proxy is None:
                        raise RuntimeError(
                            "force_gateway=True but DataImpulse gateway is not configured"
                        )
                    lease = ProxyLease(proxy=gateway_proxy, tenant_id=tenant_id)
                elif strategy == "paid_only":
                    # Skips pm.get_proxy() entirely — the scored free pool never
                    # enters the picture for this level under paid_only. Bad
                    # config was already caught at Worker.__init__ time, so a
                    # None here is unreachable; the raise is defense in depth,
                    # never a silent fallback to the free pool.
                    gateway_proxy = self._new_gateway_proxy()
                    if gateway_proxy is None:
                        raise RuntimeError(
                            "dataimpulse strategy=paid_only but gateway is not configured"
                        )
                    lease = ProxyLease(proxy=gateway_proxy, tenant_id=tenant_id)
                else:
                    try:
                        lease = await pm.get_proxy(tenant_id, level=level, domain=domain)
                    except ProxyPoolExhaustedError:
                        if strategy == "free_first" and not gateway_refused:
                            gateway_refused = await self._gateway_health.refusal() is not None
                        if strategy == "free_first" and gateway_refused:
                            gateway_bypassed = True
                        if strategy == "free_first" and not gateway_refused:
                            gateway_proxy = self._new_gateway_proxy()
                            if gateway_proxy is None:
                                raise RuntimeError(
                                    "dataimpulse strategy=free_first but gateway is not configured"
                                ) from None
                            lease = ProxyLease(proxy=gateway_proxy, tenant_id=tenant_id)
                        else:
                            return FetchResult(
                                url=url,
                                success=False,
                                level_used=level,
                                duration_ms=0,
                                failure_category=FailureCategory.PROXY_EXHAUSTED,
                                error_message="Proxy pool exhausted"
                                + (f" ({_GATEWAY_REFUSED_MESSAGE})" if gateway_refused else ""),
                                paid_gateway_skipped=gateway_bypassed or None,
                            )
                async with lease:
                    fetcher = build_fetcher()
                    result: FetchResult = await fetcher.fetch(
                        url, tenant_id, proxy=lease.proxy, overrides=overrides
                    )
                    result.proxy_source = lease.proxy.source
                    if gateway_bypassed:
                        result.paid_gateway_skipped = True
                    if lease.proxy.source == "paid_gateway":
                        if result.failure_category == FailureCategory.PROXY_AUTH_FAILED:
                            # Round 68 — the account, not the URL: tell every
                            # worker, then (free_first) make this same attempt
                            # on the free pool instead of ending the URL.
                            await self._gateway_health.mark_refused(
                                result.error_message or "proxy authentication failed"
                            )
                            gateway_refused = True
                            if strategy == "free_first":
                                continue
                        elif result.success:
                            await self._gateway_health.clear()
                    # A paid-gateway lease has no proxy_pool row (see
                    # proxy/paid_gateway.py) — mark_success/mark_failure would be
                    # a harmless no-op UPDATE either way, but gating on source
                    # makes that intent explicit instead of relying on an
                    # incidental 0-row match.
                    # Round 62 — the block check runs BEFORE the success
                    # short-circuit on purpose. At L3 a Cloudflare interstitial
                    # comes back as success=True with http_status=403 (see
                    # level_3.py's closing comment: the fetcher deliberately
                    # defers that verdict to process_job's centralized
                    # is_challenge_page check). Returning early on
                    # result.success would hand that "successful" 403 straight
                    # back and rotation would never fire for the single most
                    # common block shape there is — which is exactly what the
                    # Jumia run hit.
                    blocked = self._looks_blocked(result)
                    rotate = lease.proxy.source == "paid_gateway" and block_rotations_left > 0
                    if blocked and rotate:
                        block_rotations_left -= 1
                        continue  # new sessid on the next pass == new exit IP

                    if result.success:
                        if lease.proxy.source == "pool":
                            await pm.mark_success(tenant_id, lease.proxy.ip, lease.proxy.port)
                        return result
                    if lease.proxy.source == "pool":
                        await pm.mark_failure(tenant_id, lease.proxy.ip, lease.proxy.port, domain)
                    # Round 66 — a free proxy refusing our credentials is that
                    # proxy's fault, so a fresh lease can help. The gateway
                    # refusing them is the account's: every new session gets
                    # the same 407, so it returns at once (see process_job).
                    # Round 68 — only under paid_only; free_first already went
                    # back to the pool above.
                    retryable = result.failure_category in _PROXY_RETRYABLE_CATEGORIES or (
                        result.failure_category == FailureCategory.PROXY_AUTH_FAILED
                        and lease.proxy.source == "pool"
                    )
                    if retryable and pool_retries_left > 0:
                        pool_retries_left -= 1
                        continue  # loop again with a freshly leased proxy
                    return result

    @contextlib.asynccontextmanager
    async def _render_claim(
        self, admission: _RenderAdmission | None, weight: float
    ) -> AsyncIterator[None]:
        """Hold one host-capacity claim around one render (round 65); a no-op
        without host admission. Raises the orchestrator/host_capacity.py
        AdmissionError subclasses, which process_job turns into a
        CAPACITY_TIMEOUT / DEPENDENCY_UNAVAILABLE result."""
        if admission is None or self._admission is None:
            yield
            return
        async with self._admission.claim(
            tenant_id=admission.tenant_id,
            domain=admission.domain,
            weight=weight,
            concurrency=admission.concurrency,
            delay_seconds=admission.delay_seconds,
            priority_ms=admission.priority_ms,
            wait_budget_seconds=admission.wait_budget(),
            is_cancelled=admission.is_cancelled,
        ) as grant:
            admission.timings["admission_wait_ms"] = (
                admission.timings.get("admission_wait_ms", 0) + grant.wait_ms
            )
            yield

    def _new_gateway_proxy(self) -> Proxy | None:
        """Build a gateway Proxy pinned to a brand-new sticky session.

        Round 62. Every call returns a credential DataImpulse has never
        seen, which is what makes it hand back a different exit IP
        (proxy/paid_gateway.py's module docstring has the protocol detail).
        Before this, _fetch_with_proxy called build_gateway_proxy() with no
        arguments and got one fixed username back, so a "retry through the
        gateway" re-presented the identity that had just been blocked —
        the engine had no way to ask for a different IP even though it had
        correctly detected it needed one.
        """
        from scraper_engine.proxy.paid_gateway import build_gateway_proxy, new_session_id

        return build_gateway_proxy(
            country=self._config.dataimpulse.country or None,
            session_id=new_session_id(),
            asn=self._config.dataimpulse.asn,
        )

    def _looks_blocked(self, result: FetchResult) -> bool:
        """True when a fetch result is a target-side block, in either of the
        two shapes one takes.

        Round 62 — factored out of process_job's gateway-fallback branch so
        _fetch_with_proxy's rotation decision uses the identical test rather
        than a second, drifting copy of it. The two shapes (fetcher-level
        DETECTION_BLOCK vs. a success=True result whose CONTENT is a
        challenge page) are documented at that call site.
        """
        if result.failure_category in _GATEWAY_ROTATE_CATEGORIES:
            return True
        return bool(
            result.success
            and self._challenge_detector.is_challenge_page(
                result.html or "",
                result.http_status or 200,
                short_page_is_suspect=False,
            )
        )

    async def _acquire_politeness_slot(
        self, domain: str, tenant_id: TenantId, *, concurrency: int | None = None
    ) -> tuple[str | None, int]:
        """Retry acquiring a politeness slot for up to
        politeness.slot_wait_timeout_seconds before giving up.

        Returns (worker_id, waited_ms). worker_id is None only when the whole
        budget elapsed without a slot ever coming free, which the caller
        treats as terminal for this URL — see process_job. waited_ms is
        returned either way so the wait shows up in the result's timings
        instead of being invisible (round 63).

        Round 61 fix: the slot pool is keyed by domain+tenant only, shared
        across all 3 fetch levels — not per-level. A busy slot means "wait
        for a concurrent sibling to finish," not "this level failed, try the
        next one." The previous single-attempt-then-advance-to-next-level
        behavior let a URL burn through L1->L2->L3 in ~3s of napping under
        contention (e.g. 5 concurrent same-domain URLs racing a
        default_concurrency=2 slot pool) without ever making one real fetch
        attempt, then permanently DLQ as "no attempt ever made" — live-caught
        via 6 real DLQ entries during the round-61 investigation. Round 63
        finished that fix: the budget is no longer shorter than a single
        Level-3 attempt, and running it out no longer advances a level.
        """
        cfg = self._config.politeness
        started = time.monotonic()
        deadline = started + cfg.slot_wait_timeout_seconds
        while True:
            slot_worker_id = await self._politeness.acquire_slot(
                domain, tenant_id, concurrency=concurrency
            )
            waited_ms = int((time.monotonic() - started) * 1000)
            if slot_worker_id is not None:
                return slot_worker_id, waited_ms
            if time.monotonic() >= deadline:
                return None, waited_ms
            await asyncio.sleep(cfg.slot_retry_interval_seconds)

    @staticmethod
    def _extract_domain(url: str) -> str:
        from urllib.parse import urlparse

        parsed = urlparse(url)
        return parsed.hostname or "unknown"
