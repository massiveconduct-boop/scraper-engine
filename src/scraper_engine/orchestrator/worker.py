# orchestrator/worker.py
"""RQ task definition, execution loop, state machine driver.

The worker dequeues jobs and drives the escalation state machine:
  PENDING → CIRCUIT_CHECK → FETCHING_L1 → PARSING_L1
                                          ↘ failure → ESCALATING_L2 → ...
                                                                       ↘ DEAD_LETTER
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from scraper_engine.core.models import FailureCategory, FetchResult, JobStatus, JobStatusResponse
from scraper_engine.fetcher._failure import classify_http_status

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from pydantic import HttpUrl

    from scraper_engine.browser.botasaurus_pool import BotasaurusPool
    from scraper_engine.browser.pool import BrowserPool
    from scraper_engine.config.schema import AppConfig
    from scraper_engine.core.models import ConfigOverrides, ScrapeRequest
    from scraper_engine.core.tenant import TenantId
    from scraper_engine.storage.dlq import DeadLetterQueue
    from scraper_engine.storage.postgres_client import PostgresClient
    from scraper_engine.storage.redis_client import RedisClient

    from .circuit_breaker import CircuitBreaker
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
    }
)
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
    ) -> None:
        self._redis = redis
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
        # Circuit breaker and politeness use raw Redis (not tenant-scoped),
        # so pass the underlying client for system-level key operations
        if hasattr(circuit_breaker, "_redis"):
            pass  # already set by caller
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

    async def process_job(
        self,
        tenant_id: TenantId,
        job_id: str,
        request: ScrapeRequest,
        on_result: Callable[[FetchResult], Awaitable[None]] | None = None,
    ) -> JobStatusResponse:
        """Execute the full escalation state machine for a job.

        on_result (round 29), when given, is awaited once for every URL that
        reaches a terminal outcome (success, a DLQ'd failure, or a cache
        hit) — orchestrator/tasks.py threads this in to persist each result
        to Postgres/S3 as it lands, instead of batching everything until the
        whole job finishes. This is also what makes real per-URL progress
        and mid-job cancellation possible (see _is_cancelled below)."""
        errors: list[str] = []
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

        async def _dispatch_one_url(index: int, url: HttpUrl) -> None:
            async with semaphore:
                await _process_one_url(index, url)

        async def _process_one_url(index: int, url: HttpUrl) -> None:
            url_str = str(url)

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

            if not bypass_cache:
                cached = await self._check_cache(tenant_id, url_str)
                if cached is not None:
                    results[index] = cached
                    if on_result is not None:
                        await on_result(cached)
                    return

            domain = self._extract_domain(url_str)
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

                for level in LEVELS:
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
                    # DLQ, no attempt made.
                    if circuit_open and not self._gateway_fallback_eligible:
                        circuit_result = FetchResult(
                            url=url_str,
                            success=False,
                            level_used=level,
                            duration_ms=0,
                            failure_category=FailureCategory.CIRCUIT_OPEN,
                            error_message=f"Circuit open for {domain}",
                        )
                        await self._dlq.enqueue(
                            tenant_id,
                            job_id,
                            url_str,
                            FailureCategory.CIRCUIT_OPEN,
                            f"Circuit open for {domain}",
                            level,
                        )
                        errors.append(f"Circuit open for {domain}")
                        results[index] = circuit_result
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

                    slot_worker_id = await self._politeness.acquire_slot(domain, tenant_id)
                    if slot_worker_id is None:
                        await asyncio.sleep(1)
                        continue

                    try:
                        await self._politeness.wait_if_needed(domain, tenant_id)
                        result = await self._fetch_url(
                            tenant_id,
                            url_str,
                            level,
                            request.config_overrides,
                            force_gateway=circuit_open,
                        )
                    finally:
                        await self._politeness.release_slot(domain, tenant_id, slot_worker_id)

                    if result is None:
                        continue

                    last_level_result = result

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
                    # of this retry never fired for it at all. Only at the
                    # final level (retrying a non-final level's block is
                    # pointless — it's about to escalate anyway) and never
                    # on a result that already came from the gateway (one
                    # extra attempt per URL per level, never a second).
                    if (
                        level == LEVELS[-1]
                        and self._gateway_fallback_eligible
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
                    ):
                        gateway_result = await self._fetch_url(
                            tenant_id,
                            url_str,
                            level,
                            request.config_overrides,
                            force_gateway=True,
                        )
                        if gateway_result is not None:
                            result = gateway_result
                            last_level_result = result

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
                        still_looks_blocked = result.is_challenge_page or (
                            self._challenge_detector.looks_javascript_gated(result.html or "")
                        )
                        if level < LEVELS[-1] and still_looks_blocked:
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
                            result.error_message = result.error_message or (
                                f"Still blocked/not-found after final level "
                                f"(http_status={result.http_status})"
                            )
                            continue
                        if result.html:
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
                                extracted = await AdaptiveSelector().extract(
                                    result.html, schema=schema
                                )
                            result.extracted = extracted
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
                        results[index] = result
                        if on_result is not None:
                            await on_result(result)
                        break
                    else:
                        await self._circuit_breaker.record_failure(domain)
                        if result.failure_category in DLQ_ELIGIBLE_CATEGORIES:
                            await self._dlq.enqueue(
                                tenant_id,
                                job_id,
                                url_str,
                                result.failure_category,
                                result.error_message or "",
                                level,
                            )
                            errors.append(result.error_message or "DLQ")
                            results[index] = result
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
                    # label only in the one genuinely-unattempted case (every
                    # level's politeness slot stayed busy, so `result` was
                    # never assigned at all this URL).
                    if last_level_result is not None:
                        real_category = last_level_result.failure_category or (
                            FailureCategory.PROXY_EXHAUSTED
                        )
                        real_message = (
                            last_level_result.error_message or "All fetch levels exhausted"
                        )
                    else:
                        real_category = FailureCategory.PROXY_EXHAUSTED
                        real_message = (
                            "All fetch levels exhausted without a single attempt "
                            "(politeness slot never available)"
                        )
                    exhausted_result = FetchResult(
                        url=url_str,
                        success=False,
                        level_used=LEVELS[-1],
                        duration_ms=0,
                        failure_category=real_category,
                        error_message=real_message,
                        # Round 49 — carried forward so a DLQ'd/exhausted
                        # result stays traceable to whether the last real
                        # attempt went through the free pool or the paid
                        # gateway, instead of silently dropping it the way
                        # this branch already drops http_status/proxy_used.
                        proxy_source=(
                            last_level_result.proxy_source if last_level_result else None
                        ),
                    )
                    await self._dlq.enqueue(
                        tenant_id,
                        job_id,
                        url_str,
                        real_category,
                        real_message,
                        LEVELS[-1],
                    )
                    errors.append(real_message)
                    results[index] = exhausted_result
                    if on_result is not None:
                        await on_result(exhausted_result)
            except Exception as exc:
                logger.exception(
                    "process_job_unexpected_url_failure job_id=%s url=%s",
                    job_id,
                    url_str,
                )
                await self._circuit_breaker.record_failure(domain)
                crash_result = FetchResult(
                    url=url_str,
                    success=False,
                    level_used=LEVELS[-1],
                    duration_ms=0,
                    failure_category=FailureCategory.PARSE_ERROR,
                    error_message=f"Unexpected error processing URL: {exc}",
                )
                await self._dlq.enqueue(
                    tenant_id,
                    job_id,
                    url_str,
                    FailureCategory.PARSE_ERROR,
                    crash_result.error_message or "",
                    LEVELS[-1],
                )
                errors.append(crash_result.error_message or "Unexpected error")
                results[index] = crash_result
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
                )

            return await self._fetch_with_proxy(
                tenant_id, url, level, overrides, self._pg, _build_l2, force_gateway=force_gateway
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
                tenant_id, url, level, overrides, self._pg, _build_l3, force_gateway=force_gateway
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
        not something this method re-derives."""
        from scraper_engine.core.exceptions import ProxyPoolExhaustedError
        from scraper_engine.proxy.lease import ProxyLease
        from scraper_engine.proxy.manager import ProxyManager
        from scraper_engine.proxy.paid_gateway import build_gateway_proxy

        # Round 40 — three-way toggle (config/schema.py::DataImpulseConfig).
        # free_only is the default and is byte-for-byte the pre-round-40 code
        # path below (pm.get_proxy() every attempt, no gateway involved).
        di_cfg = self._config.dataimpulse
        strategy = di_cfg.strategy if di_cfg.enabled else "free_only"

        pm = ProxyManager(redis=self._redis, pg=pg, tier_config=self._config.proxy_tiers)
        domain = self._extract_domain(url)
        last_result: FetchResult | None = None

        for _attempt in range(_SAME_LEVEL_PROXY_RETRIES + 1):
            lease: ProxyLease
            if force_gateway:
                gateway_proxy = build_gateway_proxy()
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
                gateway_proxy = build_gateway_proxy()
                if gateway_proxy is None:
                    raise RuntimeError(
                        "dataimpulse strategy=paid_only but gateway is not configured"
                    )
                lease = ProxyLease(proxy=gateway_proxy, tenant_id=tenant_id)
            else:
                try:
                    lease = await pm.get_proxy(tenant_id, level=level, domain=domain)
                except ProxyPoolExhaustedError:
                    if strategy == "free_first":
                        gateway_proxy = build_gateway_proxy()
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
                            error_message="Proxy pool exhausted",
                        )
            async with lease:
                fetcher = build_fetcher()
                result: FetchResult = await fetcher.fetch(
                    url, tenant_id, proxy=lease.proxy, overrides=overrides
                )
                result.proxy_source = lease.proxy.source
                # A paid-gateway lease has no proxy_pool row (see
                # proxy/paid_gateway.py) — mark_success/mark_failure would be
                # a harmless no-op UPDATE either way, but gating on source
                # makes that intent explicit instead of relying on an
                # incidental 0-row match.
                if result.success:
                    if lease.proxy.source == "pool":
                        await pm.mark_success(tenant_id, lease.proxy.ip, lease.proxy.port)
                    return result
                if lease.proxy.source == "pool":
                    await pm.mark_failure(tenant_id, lease.proxy.ip, lease.proxy.port, domain)
                last_result = result
                if result.failure_category not in _PROXY_RETRYABLE_CATEGORIES:
                    return result
                # else: loop again with a freshly leased proxy — for the
                # gateway, build_gateway_proxy() returns the same static
                # ip:port, but DataImpulse rotates the real exit IP
                # server-side per connection (Rotating mode), so this retry
                # still gets a genuinely different upstream identity.

        assert last_result is not None  # loop always assigns it before falling through
        return last_result

    @staticmethod
    def _extract_domain(url: str) -> str:
        from urllib.parse import urlparse

        parsed = urlparse(url)
        return parsed.hostname or "unknown"
