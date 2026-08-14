# api/routes.py
"""API route definitions — fully wired with auth, SSRF guard, and quota.

Endpoints:
  POST   /v1/scrape        — single/multi-URL scrape (SSRF-guarded, quota-checked)
  POST   /v1/crawl         — bulk Scrapy crawl for target sets >500 URLs
  GET    /v1/jobs/{id}     — job status from live DB
  GET    /v1/jobs/{id}/dlq — raw dead-letter detail for a job
  DELETE /v1/jobs/{id}     — cancel a PENDING/PROCESSING job
  GET    /v1/health        — composite health check
"""

from __future__ import annotations

import json
import logging
import uuid

from fastapi import APIRouter, FastAPI, Header, HTTPException, Response

from scraper_engine.config.schema import AppConfig
from scraper_engine.core.models import (
    CrawlRequest,
    DeadLetterEntryResponse,
    FailureCategory,
    FetchResult,
    JobStatus,
    JobStatusResponse,
    ScrapeRequest,
)

router = APIRouter(prefix="/v1")
logger = logging.getLogger(__name__)

_TERMINAL_STATUSES = frozenset(
    {JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED, JobStatus.DEAD_LETTER}
)

_SCRAPE_JOB_TIMEOUT_SECONDS = 600
_CRAWL_JOB_TIMEOUT_SECONDS = 1800  # bulk crawls run longer than a bounded scrape job


def _validate_uuid(value: str, name: str = "id") -> str:
    """Raise 422 if value is not a valid UUID."""
    try:
        uuid.UUID(value)
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail=f"Invalid {name}: '{value}' is not a valid UUID",
        ) from None
    return value


@router.post("/scrape")
async def scrape(
    request: ScrapeRequest,
    x_api_key: str = Header(..., alias="X-API-Key"),
    idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
) -> dict[str, object]:
    """Enqueue a scrape job with SSRF validation, tenant auth, and quota check."""
    from scraper_engine.api.dependencies import (
        _queue,
        _ssrf_guard,
        _storage_pg,
        _storage_redis,
        _tenant_resolver,
    )
    from scraper_engine.core.exceptions import AuthenticationError, SSRFBlockedError

    # Tenant resolution
    if _tenant_resolver is None:
        raise HTTPException(status_code=503, detail="Service not initialized")
    try:
        tenant_id = await _tenant_resolver.resolve(x_api_key)
    except AuthenticationError:
        raise HTTPException(status_code=401, detail="Invalid API key") from None

    # SSRF validation on every URL — shared singleton so ssrf_guard.
    # additional_denied_cidrs (config) actually takes effect (see api/main.py lifespan).
    # Partitioned rather than rejecting the whole batch on the first blocked
    # URL (round 33) — 1 bad address in a 500-URL batch used to 403 the
    # entire request. Only reject outright when every URL is blocked; valid
    # URLs still proceed, and each blocked one still gets a real per-URL
    # failure result — the escalation pipeline re-validates every URL again
    # before its first request (fetcher/level_1.py's own ssrf_guard.validate
    # call) and already turns that into a FailureCategory.SSRF_BLOCKED
    # FetchResult without crashing (see fetcher/_failure.py), the same path
    # a URL that only redirects into a private range after enqueue already
    # goes through. Reusing that existing, tested machinery here instead of
    # duplicating it avoids a second, divergent SSRF-failure code path.
    if _ssrf_guard is None:
        raise HTTPException(status_code=503, detail="Service not initialized")
    blocked: list[SSRFBlockedError] = []
    valid_count = 0
    for url_val in request.urls:
        try:
            await _ssrf_guard.validate(str(url_val))
            valid_count += 1
        except SSRFBlockedError as exc:
            blocked.append(exc)
    if valid_count == 0:
        raise HTTPException(status_code=403, detail="; ".join(str(exc) for exc in blocked))

    # SSRF-guard the webhook URL too (round 34) — it's a POST target the
    # worker process reaches out to unattended, same class of risk as a
    # scrape target. Unlike the batch of scrape targets above (partitioned
    # so one bad URL doesn't sink the whole request), there's exactly one
    # webhook, so a blocked one rejects the whole request rather than being
    # silently dropped.
    if request.webhook is not None:
        try:
            await _ssrf_guard.validate(str(request.webhook))
        except SSRFBlockedError as exc:
            raise HTTPException(status_code=403, detail=f"webhook blocked: {exc}") from None

    # Idempotency-Key dedup (round 29) — must run BEFORE the quota charge
    # below, that ordering is the actual point of the fix: a client retry
    # after a timeout should hand back the still-live original job instead
    # of enqueuing a duplicate and double-charging quota. Excludes dead
    # terminal states (FAILED/CANCELLED/DEAD_LETTER) — a retry after one of
    # those should get a fresh attempt, not be pinned to a dead one forever.
    if idempotency_key and _storage_pg is not None:
        existing = await _storage_pg.fetchrow(
            tenant_id,
            """SELECT job_id, status FROM scrape_jobs
               WHERE idempotency_key = $1 AND status NOT IN ('FAILED', 'CANCELLED', 'DEAD_LETTER')
               ORDER BY created_at DESC LIMIT 1""",
            idempotency_key,
        )
        if existing is not None:
            return {
                "job_id": str(existing["job_id"]),
                "status": existing["status"],
                "urls": len(request.urls),
                "tenant": str(tenant_id),
            }

    # Quota enforcement
    if _storage_redis is not None and _storage_pg is not None:
        from scraper_engine.core.exceptions import QuotaExceededError
        from scraper_engine.core.quota import QuotaManager

        daily_limit = None
        row = await _storage_pg.fetchrow(
            tenant_id,
            "SELECT quota_daily_limit FROM public.tenants WHERE tenant_id = $1",
            str(tenant_id),
        )
        if row is not None:
            daily_limit = row["quota_daily_limit"]
        try:
            await QuotaManager(
                redis=_storage_redis,
                daily_limit=daily_limit,
            ).check_and_increment(tenant_id, count=valid_count)
        except QuotaExceededError:
            from scraper_engine.core.quota import seconds_until_quota_reset

            raise HTTPException(
                status_code=429,
                detail="Daily quota exceeded",
                headers={"Retry-After": str(seconds_until_quota_reset())},
            ) from None

    # Persist job
    job_id = str(uuid.uuid4())
    if _storage_pg is not None:
        config_json = json.dumps(
            request.config_overrides.model_dump() if request.config_overrides else {}
        )
        await _storage_pg.execute(
            tenant_id,
            """INSERT INTO scrape_jobs
                   (job_id, urls, config_used, status, webhook_url, idempotency_key)
               VALUES ($1::uuid, $2::text[], $3::jsonb, $4, $5, $6)""",
            job_id,
            [str(u) for u in request.urls],
            config_json,
            JobStatus.PENDING.value,
            str(request.webhook) if request.webhook else None,
            idempotency_key,
        )

        if _queue is not None:
            _queue.enqueue(
                "scraper_engine.orchestrator.tasks.run_scrape_job",
                str(tenant_id),
                job_id,
                job_id=job_id,
                job_timeout=_SCRAPE_JOB_TIMEOUT_SECONDS,
            )

    return {
        "job_id": job_id,
        "status": JobStatus.PENDING.value,
        "urls": len(request.urls),
        "blocked_urls": len(blocked),
        "tenant": str(tenant_id),
    }


@router.post("/crawl")
async def crawl(
    request: CrawlRequest,
    x_api_key: str = Header(..., alias="X-API-Key"),
    idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
) -> dict[str, object]:
    """Enqueue a bulk Scrapy crawl job — for target sets larger than /v1/scrape's
    500-URL cap. Same auth/SSRF/quota checks; runs on the same job queue/worker
    pool via a distinct code path (orchestrator/tasks.py branches on
    config_used["_job_type"])."""
    from scraper_engine.api.dependencies import (
        _queue,
        _ssrf_guard,
        _storage_pg,
        _storage_redis,
        _tenant_resolver,
    )
    from scraper_engine.core.exceptions import AuthenticationError, SSRFBlockedError

    if _tenant_resolver is None:
        raise HTTPException(status_code=503, detail="Service not initialized")
    try:
        tenant_id = await _tenant_resolver.resolve(x_api_key)
    except AuthenticationError:
        raise HTTPException(status_code=401, detail="Invalid API key") from None

    # Partitioned the same way POST /v1/scrape is (round 33) — but unlike
    # that pipeline, ScrapyAdapter has no per-URL SSRF re-check of its own
    # (it's a subprocess-isolated Scrapy spider, not the L1->L2->L3 ladder),
    # so a blocked seed can't be safely let through and left to self-reject
    # downstream — it must actually be filtered out of start_urls here.
    if _ssrf_guard is None:
        raise HTTPException(status_code=503, detail="Service not initialized")
    blocked: list[SSRFBlockedError] = []
    valid_start_urls: list[str] = []
    for url_val in request.start_urls:
        try:
            await _ssrf_guard.validate(str(url_val))
            valid_start_urls.append(str(url_val))
        except SSRFBlockedError as exc:
            blocked.append(exc)
    if not valid_start_urls:
        raise HTTPException(status_code=403, detail="; ".join(str(exc) for exc in blocked))

    # SSRF-guard the webhook URL too (round 34) — see POST /v1/scrape for
    # the same check and rationale.
    if request.webhook is not None:
        try:
            await _ssrf_guard.validate(str(request.webhook))
        except SSRFBlockedError as exc:
            raise HTTPException(status_code=403, detail=f"webhook blocked: {exc}") from None

    # Idempotency-Key dedup — same rationale as POST /v1/scrape above.
    if idempotency_key and _storage_pg is not None:
        existing = await _storage_pg.fetchrow(
            tenant_id,
            """SELECT job_id, status FROM scrape_jobs
               WHERE idempotency_key = $1 AND status NOT IN ('FAILED', 'CANCELLED', 'DEAD_LETTER')
               ORDER BY created_at DESC LIMIT 1""",
            idempotency_key,
        )
        if existing is not None:
            return {
                "job_id": str(existing["job_id"]),
                "status": existing["status"],
                "start_urls": len(request.start_urls),
                "tenant": str(tenant_id),
            }

    if _storage_redis is not None and _storage_pg is not None:
        from scraper_engine.core.exceptions import QuotaExceededError
        from scraper_engine.core.quota import QuotaManager

        daily_limit = None
        row = await _storage_pg.fetchrow(
            tenant_id,
            "SELECT quota_daily_limit FROM public.tenants WHERE tenant_id = $1",
            str(tenant_id),
        )
        if row is not None:
            daily_limit = row["quota_daily_limit"]
        try:
            await QuotaManager(
                redis=_storage_redis,
                daily_limit=daily_limit,
            ).check_and_increment(tenant_id, count=len(valid_start_urls))
        except QuotaExceededError:
            from scraper_engine.core.quota import seconds_until_quota_reset

            raise HTTPException(
                status_code=429,
                detail="Daily quota exceeded",
                headers={"Retry-After": str(seconds_until_quota_reset())},
            ) from None

    job_id = str(uuid.uuid4())
    if _storage_pg is not None:
        config_json = json.dumps(
            {
                "_job_type": "crawl",
                "spider_name": request.spider_name,
                "start_urls": valid_start_urls,
            }
        )
        await _storage_pg.execute(
            tenant_id,
            """INSERT INTO scrape_jobs
                   (job_id, urls, config_used, status, webhook_url, idempotency_key)
               VALUES ($1::uuid, $2::text[], $3::jsonb, $4, $5, $6)""",
            job_id,
            valid_start_urls,
            config_json,
            JobStatus.PENDING.value,
            str(request.webhook) if request.webhook else None,
            idempotency_key,
        )

        # Blocked seeds never reach ScrapyAdapter (filtered above), so unlike
        # /v1/scrape's blocked URLs — which still get a real per-URL result
        # once the escalation pipeline itself rejects them — these would
        # otherwise vanish with zero trace in GET /v1/jobs/{id}. Persist a
        # result row for each directly, matching the same failure shape
        # (FailureCategory.SSRF_BLOCKED, level_used=0 — blocked before any
        # level/spider ever ran).
        for blocked_exc in blocked:
            await _storage_pg.execute(
                tenant_id,
                """INSERT INTO scrape_results
                       (job_id, url, success, level_used, error_message, failure_category)
                   VALUES ($1::uuid, $2, FALSE, 0, $3, $4)""",
                job_id,
                blocked_exc.url,
                str(blocked_exc),
                FailureCategory.SSRF_BLOCKED.value,
            )

        if _queue is not None:
            _queue.enqueue(
                "scraper_engine.orchestrator.tasks.run_scrape_job",
                str(tenant_id),
                job_id,
                job_id=job_id,
                job_timeout=_CRAWL_JOB_TIMEOUT_SECONDS,
            )

    return {
        "job_id": job_id,
        "status": JobStatus.PENDING.value,
        "start_urls": len(valid_start_urls),
        "blocked_urls": len(blocked),
        "tenant": str(tenant_id),
    }


@router.get("/jobs/{job_id}")
async def get_job(
    job_id: str,
    x_api_key: str = Header(..., alias="X-API-Key"),
) -> JobStatusResponse:
    """Get the status and results of a scrape job from the live database."""
    from scraper_engine.api.dependencies import _storage_pg, _tenant_resolver
    from scraper_engine.core.exceptions import AuthenticationError

    _validate_uuid(job_id)

    if _tenant_resolver is None:
        raise HTTPException(status_code=503, detail="Service not initialized")
    try:
        tenant_id = await _tenant_resolver.resolve(x_api_key)
    except AuthenticationError:
        raise HTTPException(status_code=401, detail="Invalid API key") from None

    if _storage_pg is None:
        return JobStatusResponse(job_id=job_id, status=JobStatus.PENDING, progress=0.0)

    rows = await _storage_pg.fetch(
        tenant_id,
        "SELECT job_id, status, urls FROM scrape_jobs WHERE job_id = $1::uuid",
        job_id,
    )
    if not rows:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")

    row = rows[0]
    status = JobStatus(row["status"])

    result_rows = await _storage_pg.fetch(
        tenant_id,
        """SELECT url, success, http_status, is_challenge_page, level_used, proxy_used,
                  markdown, json_data, html_snapshot_url, time_taken_ms, error_message,
                  failure_category, extracted_at
           FROM scrape_results WHERE job_id = $1::uuid ORDER BY extracted_at""",
        job_id,
    )
    results = [
        FetchResult(
            url=r["url"],
            success=r["success"],
            http_status=r["http_status"],
            is_challenge_page=r["is_challenge_page"],
            level_used=r["level_used"],
            proxy_used=r["proxy_used"],
            markdown=r["markdown"],
            extracted=json.loads(r["json_data"]) if r["json_data"] else None,
            duration_ms=r["time_taken_ms"] or 0,
            error_message=r["error_message"],
            failure_category=(
                FailureCategory(r["failure_category"]) if r["failure_category"] else None
            ),
            html_snapshot_url=r["html_snapshot_url"],
            fetched_at=r["extracted_at"],
        )
        for r in result_rows
    ]
    errors = [r.error_message for r in results if not r.success and r.error_message]

    # Real per-URL progress (round 29) — results now persist incrementally
    # as each URL completes (see orchestrator/tasks.py's on_result
    # callback), so len(result_rows) genuinely reflects how many of the
    # job's URLs are done, not a hardcoded stand-in.
    total_urls = len(row["urls"]) or 1
    progress = 1.0 if status in _TERMINAL_STATUSES else min(1.0, len(result_rows) / total_urls)
    # Same partial_failure formula as Worker.process_job (round 34) — this
    # is a second construction site for JobStatusResponse (DB-reconstructed
    # rather than the in-memory one the worker returns), so it must be kept
    # in sync rather than assuming polling always sees the worker's copy.
    partial_failure = bool(errors) and any(r.success for r in results)

    return JobStatusResponse(
        # asyncpg returns a uuid.UUID for the UUID column; JobStatusResponse.job_id
        # is typed str, so coerce explicitly (round 16 — this 500'd on every
        # existing job, caught by the full-stack e2e smoke).
        job_id=str(row["job_id"]),
        status=status,
        progress=progress,
        results=results or None,
        error="; ".join(errors) if errors else None,
        partial_failure=partial_failure,
    )


@router.get("/jobs/{job_id}/dlq")
async def get_job_dlq(
    job_id: str,
    x_api_key: str = Header(..., alias="X-API-Key"),
) -> list[DeadLetterEntryResponse]:
    """Raw dead-letter detail for one job (round 29) — a permanently failed
    URL already gets a row in GET /v1/jobs/{job_id}'s normal `results` list
    (with error_message/failure_category), but the DLQ additionally carries
    enqueued_at/dead_at timestamps and is the basis for ops tooling
    (DeadLetterQueue.retry(), the dlq_size Prometheus gauge) — this route
    just opens a door onto detail that already existed internally."""
    from scraper_engine.api.dependencies import _storage_pg, _tenant_resolver
    from scraper_engine.core.exceptions import AuthenticationError
    from scraper_engine.storage.dlq import DeadLetterQueue

    _validate_uuid(job_id)

    if _tenant_resolver is None:
        raise HTTPException(status_code=503, detail="Service not initialized")
    try:
        tenant_id = await _tenant_resolver.resolve(x_api_key)
    except AuthenticationError:
        raise HTTPException(status_code=401, detail="Invalid API key") from None

    if _storage_pg is None:
        return []

    entries = await DeadLetterQueue(_storage_pg).list_for_tenant(tenant_id, job_id=job_id)
    return [
        DeadLetterEntryResponse(
            job_id=e.job_id,
            url=e.url,
            failure_category=e.failure_category,
            error_message=e.error_message,
            level_attempted=e.level_attempted,
            auto_retry_count=e.auto_retry_count,
            enqueued_at=e.enqueued_at,
            dead_at=e.dead_at,
        )
        for e in entries
    ]


@router.delete("/jobs/{job_id}")
async def cancel_job(
    job_id: str,
    x_api_key: str = Header(..., alias="X-API-Key"),
) -> dict[str, object]:
    """Cancel a PENDING/PROCESSING job (round 29). Best-effort: removes a
    still-queued job from rq outright; an in-flight job cooperatively checks
    for CANCELLED between URLs (Worker._is_cancelled) and stops there —
    results already persisted for prior URLs (on_result callback, see
    orchestrator/tasks.py) are kept, not rolled back."""
    from scraper_engine.api.dependencies import _queue, _storage_pg, _tenant_resolver
    from scraper_engine.core.exceptions import AuthenticationError

    _validate_uuid(job_id)

    if _tenant_resolver is None:
        raise HTTPException(status_code=503, detail="Service not initialized")
    try:
        tenant_id = await _tenant_resolver.resolve(x_api_key)
    except AuthenticationError:
        raise HTTPException(status_code=401, detail="Invalid API key") from None

    if _storage_pg is None:
        raise HTTPException(status_code=503, detail="Service not initialized")

    terminal_values = [s.value for s in _TERMINAL_STATUSES]
    row = await _storage_pg.fetchrow(
        tenant_id,
        """UPDATE scrape_jobs SET status = $1, updated_at = NOW()
           WHERE job_id = $2::uuid AND status <> ALL($3::text[])
           RETURNING status""",
        JobStatus.CANCELLED.value,
        job_id,
        terminal_values,
    )
    if row is None:
        exists = await _storage_pg.fetchrow(
            tenant_id, "SELECT 1 FROM scrape_jobs WHERE job_id = $1::uuid", job_id
        )
        if exists is None:
            raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
        raise HTTPException(status_code=409, detail="Job already in a terminal state")

    if _queue is not None:
        job = _queue.fetch_job(job_id)
        if job is not None:
            try:
                job.cancel()
            except Exception:
                logger.warning("rq_job_cancel_failed job_id=%s", job_id, exc_info=True)

    return {"job_id": job_id, "status": JobStatus.CANCELLED.value}


@router.get("/health")
async def health() -> dict[str, object]:
    """Composite health check — pg/redis/s3 reachability + proxy pool size."""
    from scraper_engine.api.dependencies import _storage_pg, _storage_redis, _storage_s3
    from scraper_engine.api.health import check_health

    if _storage_pg is None or _storage_redis is None:
        raise HTTPException(status_code=503, detail="Service not initialized")

    status = await check_health(_storage_pg, _storage_redis, _storage_s3)
    payload: dict[str, object] = {
        "status": "ok" if status.healthy else "degraded",
        "pgbouncer_reachable": status.pgbouncer_reachable,
        "redis_reachable": status.redis_reachable,
        "s3_reachable": status.s3_reachable,
        "proxy_pool_size": status.proxy_pool_size,
        "checks": status.checks,
    }
    if not status.healthy:
        raise HTTPException(status_code=503, detail=payload)
    return payload


def register_routes(app: FastAPI, cfg: AppConfig) -> None:
    """Register all API routes on the FastAPI app, including /metrics."""
    if cfg.observability.metrics_enabled:
        from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

        @app.get("/metrics")
        async def metrics() -> Response:
            from prometheus_client import REGISTRY

            from scraper_engine.api import dependencies
            from scraper_engine.core.tenant import TenantId
            from scraper_engine.observability.metrics import (
                count_validated_proxies,
                proxy_pool_validated_count,
                refresh_capsolver_spend,
                refresh_dlq_size,
                refresh_proxy_source_health,
                refresh_redis_backed_counters,
            )

            pg = dependencies._storage_pg
            redis = dependencies._storage_redis
            if pg is not None:
                try:
                    tenant = TenantId("system")
                    count = await count_validated_proxies(pg, tenant)
                    proxy_pool_validated_count.set(count)
                except Exception:
                    logger.warning("proxy_pool_validated_count gauge update failed", exc_info=True)
                try:
                    await refresh_dlq_size(pg)
                except Exception:
                    logger.warning("dlq_size gauge update failed", exc_info=True)
                if redis is not None:
                    try:
                        await refresh_capsolver_spend(pg, redis)
                    except Exception:
                        logger.warning("capsolver spend gauge update failed", exc_info=True)
            if redis is not None:
                try:
                    await refresh_redis_backed_counters(redis)
                except Exception:
                    logger.warning("redis-backed counter refresh failed", exc_info=True)
                try:
                    await refresh_proxy_source_health(redis)
                except Exception:
                    logger.warning("proxy_source_healthy gauge update failed", exc_info=True)

            return Response(content=generate_latest(REGISTRY), media_type=CONTENT_TYPE_LATEST)

    app.include_router(router)
