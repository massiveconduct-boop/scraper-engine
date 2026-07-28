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
from typing import TYPE_CHECKING, Any

from opentelemetry import trace

from config.loader import load_config
from core.models import ConfigOverrides, FetchResult, JobStatus, JobStatusResponse, ScrapeRequest
from core.tenant import TenantId
from observability.bootstrap import bootstrap_observability

if TYPE_CHECKING:
    from config.schema import AppConfig
    from storage.postgres_client import PostgresClient
    from storage.redis_client import RedisClient
    from storage.s3_client import S3Client

logger = logging.getLogger(__name__)

# rq imports this module once per worker process before executing any job —
# module-level code is the right place to bootstrap once, rather than
# reconfiguring logging/tracing on every single job inside _run_scrape_job.
bootstrap_observability(load_config().observability)


def run_scrape_job(tenant_id: str, job_id: str) -> None:
    """Sync entry point rq calls. Runs the async pipeline to completion."""
    asyncio.run(_run_scrape_job(tenant_id, job_id))


async def _run_scrape_job(tenant_id_raw: str, job_id: str) -> None:
    from storage.postgres_client import PostgresClient
    from storage.redis_client import RedisClient
    from storage.s3_client import S3Client

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
    try:
        with tracer.start_as_current_span(
            "scrape_job", attributes={"job_id": job_id, "tenant_id": tenant_id_raw}
        ):
            row = await pg.fetchrow(
                tenant_id,
                "SELECT urls, config_used, webhook_url FROM scrape_jobs WHERE job_id = $1::uuid",
                job_id,
            )
            if row is None:
                logger.error("job_not_found job_id=%s tenant=%s", job_id, tenant_id)
                return

            config_used: dict[str, Any] = (
                json.loads(row["config_used"]) if row["config_used"] else {}
            )
            webhook_url: str | None = row["webhook_url"]

            await pg.execute(
                tenant_id,
                "UPDATE scrape_jobs SET status = $1, updated_at = NOW() WHERE job_id = $2::uuid",
                JobStatus.PROCESSING.value,
                job_id,
            )

            status: JobStatus
            error: str | None
            if config_used.get("_job_type") == "crawl":
                results = await _run_crawl_job(config_used)
                status = JobStatus.COMPLETED
                error = None
            else:
                request = ScrapeRequest(
                    urls=list(row["urls"]),
                    config_overrides=ConfigOverrides(**config_used) if config_used else None,
                )
                response = await _run_scrape(tenant_id, job_id, request, redis, pg, cfg)
                results = response.results or []
                status = response.status
                error = response.error

            await _persist_results(pg, s3, tenant_id, job_id, results)

            await pg.execute(
                tenant_id,
                "UPDATE scrape_jobs SET status = $1, updated_at = NOW() WHERE job_id = $2::uuid",
                status.value,
                job_id,
            )

            if webhook_url:
                await _dispatch_webhook(webhook_url, job_id, status, results, error)
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
    cfg: AppConfig,
) -> JobStatusResponse:
    from orchestrator.circuit_breaker import CircuitBreaker
    from orchestrator.politeness import PolitenessController
    from orchestrator.worker import Worker
    from storage.dlq import DeadLetterQueue

    circuit_breaker = CircuitBreaker(
        redis.raw,
        failure_threshold=cfg.circuit_breaker.failure_threshold,
        attempt_threshold=cfg.circuit_breaker.attempt_threshold,
        cooldown_seconds=cfg.circuit_breaker.cooldown_seconds,
        max_cooldown_seconds=cfg.circuit_breaker.max_cooldown_seconds,
    )
    politeness = PolitenessController(
        redis.raw,
        default_concurrency=cfg.politeness.default_concurrency,
        default_delay_seconds=cfg.politeness.default_delay_seconds,
        slot_ttl_seconds=cfg.politeness.slot_ttl_seconds,
    )
    dlq = DeadLetterQueue(pg)
    worker = Worker(
        redis=redis, circuit_breaker=circuit_breaker, politeness=politeness, dlq=dlq, config=cfg
    )
    return await worker.process_job(tenant_id, job_id, request)


async def _run_crawl_job(config_used: dict[str, Any]) -> list[FetchResult]:
    from services.scrapy_adapter import ScrapyAdapter

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


async def _persist_results(
    pg: PostgresClient,
    s3: S3Client,
    tenant_id: TenantId,
    job_id: str,
    results: list[FetchResult],
) -> None:
    for result in results:
        html_snapshot_url = None
        if result.html:
            html_snapshot_url = await s3.store_snapshot(
                tenant_id, job_id, result.url, result.html, result.success
            )
        content_source = result.html or result.markdown or ""
        content_hash = (
            hashlib.sha256(content_source.encode("utf-8")).hexdigest() if content_source else None
        )

        await pg.execute(
            tenant_id,
            """
            INSERT INTO scrape_results
                (job_id, url, success, http_status, is_challenge_page, level_used,
                 proxy_used, markdown, json_data, html_snapshot_url, content_hash,
                 time_taken_ms, error_message, failure_category)
            VALUES ($1::uuid, $2, $3, $4, $5, $6, $7, $8, $9::jsonb, $10, $11, $12, $13, $14)
            """,
            job_id,
            result.url,
            result.success,
            result.http_status,
            result.is_challenge_page,
            result.level_used,
            result.proxy_used,
            result.markdown,
            json.dumps(result.extracted) if result.extracted is not None else None,
            html_snapshot_url,
            content_hash,
            result.duration_ms,
            result.error_message,
            result.failure_category.value if result.failure_category else None,
        )


async def _dispatch_webhook(
    webhook_url: str,
    job_id: str,
    status: JobStatus,
    results: list[FetchResult],
    error: str | None,
) -> None:
    from orchestrator.webhook import WebhookDispatcher

    payload = JobStatusResponse(
        job_id=job_id, status=status, progress=1.0, results=results or None, error=error
    )
    try:
        delivered = await WebhookDispatcher().deliver(webhook_url, payload)
        if not delivered:
            logger.warning("webhook_delivery_failed job_id=%s url=%s", job_id, webhook_url)
    except Exception:
        logger.exception("webhook_delivery_error job_id=%s url=%s", job_id, webhook_url)
