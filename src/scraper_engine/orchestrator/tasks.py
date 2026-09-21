# orchestrator/tasks.py
"""rq task entry point — the missing link between the queue and Worker.process_job.

``run_scrape_job`` is the dotted import path rq workers resolve
(``rq worker scraper-jobs`` in docker-compose.yml). rq's worker loop calls job
functions synchronously, so this wraps the actual async pipeline in
``asyncio.run``. One job function handles both job types stored on
``scrape_jobs.config_used``:

  - normal scrape jobs: drive ``Worker.process_job`` (the L1->L2->L3
    escalation ladder), one row per URL.
  - bulk crawl jobs (``config_used["_job_type"] == "crawl"``): drive
    ``ScrapyAdapter.run_spider`` instead (see api/routes.py POST /v1/crawl).

Either way this function is the single place that persists results back to
Postgres (``scrape_jobs``/``scrape_results``), stores HTML snapshots to S3,
and fires the tenant's webhook — none of which ``Worker.process_job`` itself
does (it only returns an in-memory ``JobStatusResponse``).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from typing import TYPE_CHECKING, Any

from opentelemetry import trace

from scraper_engine.config.loader import load_config
from scraper_engine.core.budget import configure_budget, resolve_browser_max_total_instances
from scraper_engine.core.models import (
    ConfigOverrides,
    FetchResult,
    JobStatus,
    JobStatusResponse,
    ScrapeRequest,
)
from scraper_engine.core.tenant import TenantId
from scraper_engine.observability.bootstrap import bootstrap_observability
from scraper_engine.orchestrator.webhook_events import WebhookEventType

if TYPE_CHECKING:
    from scraper_engine.config.schema import AppConfig
    from scraper_engine.storage.dlq import DeadLetterQueue
    from scraper_engine.storage.postgres_client import PostgresClient
    from scraper_engine.storage.redis_client import RedisClient
    from scraper_engine.storage.s3_client import S3Client

logger = logging.getLogger(__name__)

# rq imports this module once per worker process before executing any job —
# module-level code is the right place to bootstrap once, rather than
# reconfiguring logging/tracing (or resizing the budget semaphores) on every
# single job inside _run_scrape_job.
_bootstrap_cfg = load_config()
bootstrap_observability(_bootstrap_cfg.observability)
configure_budget(
    browser_max_total_instances=resolve_browser_max_total_instances(
        _bootstrap_cfg.camoufox.max_total_instances,
        enabled=_bootstrap_cfg.camoufox.ram_aware_concurrency_enabled,
        average_ram_per_instance_gb=_bootstrap_cfg.camoufox.ram_aware_avg_instance_gb,
    ),
    capsolver_max_concurrent_solves=_bootstrap_cfg.capsolver.max_concurrent_solves,
)


def run_scrape_job(tenant_id: str, job_id: str) -> None:
    """Sync entry point rq calls. Runs the async pipeline to completion."""
    asyncio.run(_run_scrape_job(tenant_id, job_id))


async def _run_scrape_job(tenant_id_raw: str, job_id: str) -> None:
    from scraper_engine.storage.postgres_client import PostgresClient
    from scraper_engine.storage.redis_client import RedisClient
    from scraper_engine.storage.s3_client import S3Client

    cfg = load_config()
    tenant_id = TenantId(tenant_id_raw)

    pg = PostgresClient(cfg.storage.database_url)
    redis = RedisClient(redis_url=cfg.storage.redis_url)
    s3 = S3Client(
        endpoint_url=cfg.s3.endpoint_url,
        access_key=cfg.s3.access_key,
        secret_key=cfg.s3.secret_key,
        bucket=cfg.s3.bucket,
    )

    await pg.start()
    await redis.start()
    await s3.start()
    tracer = trace.get_tracer(__name__)
    job_start = time.monotonic()
    # Hoisted so the except block below can reference it even if the crash
    # happens before the row fetch assigns a real value — None means there's
    # nothing to notify, correctly skipping the webhook dispatch.
    webhook_url: str | None = None
    try:
        with tracer.start_as_current_span(
            "scrape_job", attributes={"job_id": job_id, "tenant_id": tenant_id_raw}
        ):
            row = await pg.fetchrow(
                tenant_id,
                """SELECT urls, config_used, webhook_url, status
                   FROM scrape_jobs WHERE job_id = $1::uuid""",
                job_id,
            )
            if row is None:
                logger.error("job_not_found job_id=%s tenant=%s", job_id, tenant_id)
                return

            if row["status"] == JobStatus.CANCELLED.value:
                # A DELETE /v1/jobs/{job_id} raced ahead of rq dequeuing this
                # job (round 29) — honor the cancel rather than silently
                # overwriting it back to PROCESSING below.
                logger.info("job_already_cancelled job_id=%s tenant=%s", job_id, tenant_id)
                return

            config_used: dict[str, Any] = (
                json.loads(row["config_used"]) if row["config_used"] else {}
            )
            webhook_url = row["webhook_url"]

            # started_at (round 63) is set exactly once, here, where the
            # job stops waiting and starts running: created_at -> started_at
            # IS the queue wait, which was previously underivable because
            # updated_at is overwritten by every later transition.
            await pg.execute(
                tenant_id,
                "UPDATE scrape_jobs SET status = $1, updated_at = NOW(), "
                "started_at = NOW() WHERE job_id = $2::uuid",
                JobStatus.PROCESSING.value,
                job_id,
            )

            status: JobStatus
            error: str | None
            partial_failure = False
            if config_used.get("_job_type") == "crawl":
                results = await _run_crawl_job(config_used)
                status = JobStatus.COMPLETED
                error = None
                # Batch persist — ScrapyAdapter.run_spider returns a full
                # batch, not a stream, so there's no per-item callback to
                # hook here the way the escalation path below has.
                await _persist_results(pg, s3, tenant_id, job_id, results)
            else:
                request = ScrapeRequest(
                    urls=list(row["urls"]),
                    config_overrides=ConfigOverrides(**config_used) if config_used else None,
                )
                # _run_scrape persists each result as it lands (on_result
                # callback threaded into Worker.process_job, round 29) —
                # no batch persist needed here, unlike the crawl branch above.
                response = await _run_scrape(tenant_id, job_id, request, redis, pg, s3, cfg)
                results = response.results or []
                status = response.status
                error = response.error
                partial_failure = response.partial_failure

            await pg.execute(
                tenant_id,
                "UPDATE scrape_jobs SET status = $1, updated_at = NOW(), "
                "finished_at = NOW() WHERE job_id = $2::uuid",
                status.value,
                job_id,
            )

            if webhook_url:
                await _dispatch_job_webhook(
                    cfg,
                    pg,
                    redis,
                    tenant_id,
                    webhook_url,
                    job_id,
                    status,
                    results,
                    error,
                    partial_failure,
                )

            # Redis-backed counter, refreshed into job_duration_seconds_count/_sum
            # gauges only when /metrics is actually scraped (this rq work-horse
            # process exits right after this job — see the force_flush comment
            # below for why an in-process Histogram would never be scraped).
            metric_status = "completed" if status == JobStatus.COMPLETED else "failed"
            try:
                await redis.raw.incr(f"metrics:job_duration:{metric_status}:count")
                await redis.raw.incrbyfloat(
                    f"metrics:job_duration:{metric_status}:sum",
                    time.monotonic() - job_start,
                )
            except Exception:
                logger.warning("job_duration metric update failed", exc_info=True)
    except Exception:
        # A worker-level crash (e.g. a bug in the escalation pipeline) must
        # never leave scrape_jobs.status stuck at PROCESSING forever — that
        # left GET /v1/jobs/{id} reporting a phantom in-progress job with no
        # way for a caller to detect the failure short of reading worker
        # logs. Mark FAILED and re-raise so rq's own failure bookkeeping
        # still sees the exception (marking our DB row correct must not
        # silently lie to rq's own tracking).
        logger.exception("scrape_job_crashed job_id=%s tenant=%s", job_id, tenant_id_raw)
        # finished_at is set here too (round 63): a crashed job HAS stopped
        # running, so leaving it NULL would report runtime_ms=None for the
        # very jobs whose duration a caller most wants to see.
        await pg.execute(
            tenant_id,
            "UPDATE scrape_jobs SET status = $1, updated_at = NOW(), "
            "finished_at = NOW() WHERE job_id = $2::uuid",
            JobStatus.FAILED.value,
            job_id,
        )
        if webhook_url:
            await _dispatch_job_webhook(
                cfg,
                pg,
                redis,
                tenant_id,
                webhook_url,
                job_id,
                JobStatus.FAILED,
                [],
                "internal error — see server logs",
                partial_failure=False,
            )
        raise
    finally:
        await s3.stop()
        await redis.stop()
        await pg.stop()
        # rq runs each job in a forked "work horse" process that exits via
        # os._exit() (rq/worker/base.py) — that bypasses atexit entirely, so
        # the BatchSpanProcessor's background export thread (which doesn't
        # survive fork() anyway — only the calling thread does) never gets a
        # chance to flush the span queued above. Without this, every job's
        # trace was silently dropped at process exit (confirmed live: spans
        # from a real rq worker never reached Jaeger; the identical code path
        # invoked directly, not via a forked work-horse, worked immediately).
        # hasattr guard: the default no-op provider has no force_flush at all.
        # Bounded timeout: an unreachable collector must never stall a job —
        # this is a best-effort export, not a delivery guarantee.
        provider = trace.get_tracer_provider()
        if hasattr(provider, "force_flush"):
            provider.force_flush(timeout_millis=2000)


async def _run_scrape(
    tenant_id: TenantId,
    job_id: str,
    request: ScrapeRequest,
    redis: RedisClient,
    pg: PostgresClient,
    s3: S3Client,
    cfg: AppConfig,
) -> JobStatusResponse:
    from scraper_engine.browser.botasaurus_pool import BotasaurusPool
    from scraper_engine.browser.pool import BrowserPool
    from scraper_engine.browser.session_state import SessionStateManager
    from scraper_engine.orchestrator.circuit_breaker import CircuitBreaker
    from scraper_engine.orchestrator.politeness import PolitenessController
    from scraper_engine.orchestrator.worker import Worker
    from scraper_engine.storage.dlq import DeadLetterQueue

    circuit_breaker = CircuitBreaker(
        redis.raw,
        failure_threshold=cfg.circuit_breaker.failure_threshold,
        attempt_threshold=cfg.circuit_breaker.attempt_threshold,
        cooldown_seconds=cfg.circuit_breaker.cooldown_seconds,
        max_cooldown_seconds=cfg.circuit_breaker.max_cooldown_seconds,
        failure_streak_ttl_seconds=cfg.circuit_breaker.failure_streak_ttl_seconds,
    )
    politeness = PolitenessController(
        redis.raw,
        default_concurrency=cfg.politeness.default_concurrency,
        default_delay_seconds=cfg.politeness.default_delay_seconds,
        slot_ttl_seconds=cfg.politeness.slot_ttl_seconds,
    )
    dlq = DeadLetterQueue(pg)

    # One BrowserPool per job (round 25) — rq forks a fresh "work horse" process
    # per job (see the force_flush comment below), so a pool can only ever live
    # for the duration of one job; started/shut down here the same way pg/redis/s3
    # bracket the whole job in _run_scrape_job. Still a real win for jobs with
    # multiple URLs on the same domain (crawls), which now reuse one hot browser
    # instead of cold-starting Camoufox per URL.
    session_mgr = SessionStateManager(pg, ttl_days=cfg.session_retention.browser_sessions_ttl_days)
    browser_pool = BrowserPool(
        tenant_id=tenant_id,
        session_mgr=session_mgr,
        geoip=cfg.camoufox.geoip,
        humanize=cfg.camoufox.humanize,
        headless_mode=cfg.camoufox.headless_mode,
        max_total_instances=cfg.camoufox.max_total_instances,
        fingerprint_preset=cfg.camoufox.fingerprint_preset,
        os=cfg.camoufox.os,
    )
    # One BotasaurusPool per job too (round 26), same rationale and lifetime
    # as browser_pool above — reuses one live Botasaurus driver across
    # same-domain URLs in a crawl job instead of relaunching per URL. See
    # browser/botasaurus_pool.py for why this doesn't use botasaurus's own
    # reuse_driver=True.
    botasaurus_pool = BotasaurusPool(tenant_id=tenant_id, config=cfg.botasaurus)

    async def _on_result(result: FetchResult) -> None:
        """Persist each result the moment it lands (round 29) instead of
        batching everything until the whole job finishes — see
        _persist_one_result below."""
        await _persist_one_result(pg, s3, tenant_id, job_id, result, dlq)

    # Round 51 — browser_pool.start() moved inside this try/finally. It used
    # to run before the block, so a raise from it (e.g. the prewarm_count
    # vs max_total_instances misconfiguration check) skipped
    # browser_pool.shutdown() entirely, since that shutdown only runs in
    # this finally. start() itself no longer raises on a per-instance
    # launch failure (browser/pool.py round 51), but keeping this ordering
    # correct for the config-check case that legitimately still can.
    try:
        await browser_pool.start()
        worker = Worker(
            redis=redis,
            circuit_breaker=circuit_breaker,
            politeness=politeness,
            dlq=dlq,
            config=cfg,
            pg=pg,
            browser_pool=browser_pool,
            botasaurus_pool=botasaurus_pool,
        )
        return await worker.process_job(tenant_id, job_id, request, on_result=_on_result)
    finally:
        await browser_pool.shutdown()
        await botasaurus_pool.shutdown()


async def _run_crawl_job(config_used: dict[str, Any]) -> list[FetchResult]:
    from scraper_engine.services.scrapy_adapter import ScrapyAdapter

    spider_name = str(config_used["spider_name"])
    start_urls = [str(u) for u in config_used.get("start_urls", [])]
    items = await ScrapyAdapter().run_spider(spider_name, start_urls)
    return [
        FetchResult(
            url=str(item.get("url", "")),
            success=True,
            level_used=0,
            duration_ms=0,
            extracted=item,
        )
        for item in items
    ]


async def _persist_one_result(
    pg: PostgresClient,
    s3: S3Client,
    tenant_id: TenantId,
    job_id: str,
    result: FetchResult,
    dlq: DeadLetterQueue | None = None,
) -> None:
    """Persist a single FetchResult — one scrape_results row, plus an S3
    snapshot when there's HTML to store. Split out from _persist_results
    (round 29) so the escalation path (_run_scrape) can call this per-result
    via the on_result callback instead of waiting for the whole job to
    finish. A cache-hit result (FetchResult.from_cache=True) has no `html`
    (see Worker._check_cache), so the S3 upload is skipped and the existing
    html_snapshot_url pointer it already carries is persisted as-is —
    no duplicate snapshot for content that's already stored.

    dlq (round 34): when given and the result succeeded, clears any stale
    DLQ entry for this exact (job_id, url) — the URL this row belongs to
    may have previously been DLQ'd and auto-retried by proxy/dlq_reaper.py;
    without this, a URL that recovered on retry would still show as
    permanently dead in GET /v1/jobs/{id}/dlq forever. None (the bulk-crawl
    path, _persist_results below) skips this — ScrapyAdapter results are
    always success=True and crawl jobs never populate the DLQ in the first
    place, so there's nothing to clear."""
    if dlq is not None and result.success:
        await dlq.clear(tenant_id, job_id, result.url)
    html_snapshot_url = result.html_snapshot_url
    if result.html:
        html_snapshot_url = await s3.store_snapshot(
            tenant_id, job_id, result.url, result.html, result.success
        )
        # Mutate the in-memory result so the webhook payload (built from
        # these same objects, see _dispatch_job_webhook below) carries the
        # snapshot pointer too, not just the polling response.
        result.html_snapshot_url = html_snapshot_url
    content_source = result.html or result.markdown or ""
    content_hash = (
        hashlib.sha256(content_source.encode("utf-8")).hexdigest() if content_source else None
    )

    await pg.execute(
        tenant_id,
        """
        INSERT INTO scrape_results
            (job_id, url, success, http_status, is_challenge_page, level_used,
             proxy_used, proxy_source, markdown, json_data, network_events, html_snapshot_url,
             content_hash, time_taken_ms, error_message, failure_category, timings)
        VALUES ($1::uuid, $2, $3, $4, $5, $6, $7, $8, $9, $10::jsonb, $11::jsonb, $12, $13, $14,
                $15, $16, $17::jsonb)
        """,
        job_id,
        result.url,
        result.success,
        result.http_status,
        result.is_challenge_page,
        result.level_used,
        result.proxy_used,
        result.proxy_source,
        result.markdown,
        json.dumps(result.extracted) if result.extracted is not None else None,
        json.dumps(result.network_events) if result.network_events is not None else None,
        html_snapshot_url,
        content_hash,
        result.duration_ms,
        result.error_message,
        result.failure_category.value if result.failure_category else None,
        json.dumps(result.timings) if result.timings is not None else None,
    )

    # Round 63 — touch the job row so "stale" means "not progressing" rather
    # than "started more than N seconds ago". orchestrator/stuck_job_reaper.py
    # treats a PROCESSING row whose updated_at is older than its grace window
    # as a candidate for reconciliation; updated_at was only ever written at
    # status transitions, so a long multi-URL job looked identically stale to
    # a job whose worker had died, and its only protection was the rq
    # reachability check — which had itself been comparing against a registry
    # key that does not exist in rq 2.10. Two independent signals now have to
    # fail before live work is reconciled away.
    await pg.execute(
        tenant_id,
        "UPDATE scrape_jobs SET updated_at = NOW() WHERE job_id = $1::uuid",
        job_id,
    )


async def _persist_results(
    pg: PostgresClient,
    s3: S3Client,
    tenant_id: TenantId,
    job_id: str,
    results: list[FetchResult],
) -> None:
    """Batch persist — used only by the bulk-crawl path (_run_crawl_job),
    which has no per-item callback available since ScrapyAdapter.run_spider
    returns a full batch rather than a stream. The L1->L2->L3 escalation
    path (_run_scrape) persists incrementally instead, via
    _persist_one_result threaded in as Worker.process_job's on_result
    callback."""
    for result in results:
        await _persist_one_result(pg, s3, tenant_id, job_id, result)


def _job_webhook_event_type(status: JobStatus, partial_failure: bool) -> WebhookEventType:
    """Map a job's terminal outcome to an event type — single source of
    truth so this mapping isn't re-derived at each call site (round 34)."""
    if status == JobStatus.CANCELLED:
        return WebhookEventType.JOB_CANCELLED
    if status == JobStatus.COMPLETED:
        return (
            WebhookEventType.JOB_PARTIAL_FAILURE
            if partial_failure
            else WebhookEventType.JOB_COMPLETED
        )
    return WebhookEventType.JOB_FAILED


async def _dispatch_job_webhook(
    cfg: AppConfig,
    pg: PostgresClient,
    redis: RedisClient,
    tenant_id: TenantId,
    webhook_url: str,
    job_id: str,
    status: JobStatus,
    results: list[FetchResult],
    error: str | None,
    partial_failure: bool,
) -> None:
    """Build the job's WebhookEvent and hand it to the durable outbox path
    (round 34) — replaces the old fire-and-forget POST. See
    orchestrator/webhook_events.py and storage/webhook_outbox.py."""
    from scraper_engine.orchestrator.webhook_events import WebhookEvent

    response = JobStatusResponse(
        job_id=job_id,
        status=status,
        progress=1.0,
        results=results or None,
        error=error,
        partial_failure=partial_failure,
    )
    event = WebhookEvent(
        event_type=_job_webhook_event_type(status, partial_failure),
        tenant_id=str(tenant_id),
        job_id=job_id,
        payload=response.model_dump(mode="json"),
    )
    from scraper_engine.orchestrator.webhook_dispatch import enqueue_and_deliver_webhook_event

    await enqueue_and_deliver_webhook_event(cfg, pg, redis, tenant_id, webhook_url, event)
