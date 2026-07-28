# api/routes.py
"""API route definitions — fully wired with auth, SSRF guard, and quota.

Endpoints:
  POST /v1/scrape      — single/multi-URL scrape (SSRF-guarded, quota-checked)
  POST /v1/crawl       — bulk Scrapy crawl for target sets >500 URLs
  GET  /v1/jobs/{id}   — job status from live DB
  GET  /v1/health       — composite health check
"""

from __future__ import annotations

import json
import logging
import uuid

from fastapi import APIRouter, FastAPI, Header, HTTPException, Response

from core.models import (
    CrawlRequest,
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
) -> dict[str, object]:
    """Enqueue a scrape job with SSRF validation, tenant auth, and quota check."""
    from api.dependencies import _queue, _storage_pg, _storage_redis, _tenant_resolver
    from core.exceptions import AuthenticationError, SSRFBlockedError
    from core.ssrf_guard import SSRFGuard

    # Tenant resolution
    if _tenant_resolver is None:
        raise HTTPException(status_code=503, detail="Service not initialized")
    try:
        tenant_id = await _tenant_resolver.resolve(x_api_key)
    except AuthenticationError:
        raise HTTPException(status_code=401, detail="Invalid API key") from None

    # SSRF validation on every URL
    ssrf = SSRFGuard()
    for url_val in request.urls:
        try:
            await ssrf.validate(str(url_val))
        except SSRFBlockedError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc

    # Quota enforcement
    if _storage_redis is not None and _storage_pg is not None:
        from core.exceptions import QuotaExceededError
        from core.quota import QuotaManager
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
                redis=_storage_redis, daily_limit=daily_limit,
            ).check_and_increment(tenant_id, count=len(request.urls))
        except QuotaExceededError:
            raise HTTPException(status_code=429, detail="Daily quota exceeded") from None

    # Persist job
    job_id = str(uuid.uuid4())
    if _storage_pg is not None:
        config_json = json.dumps(
            request.config_overrides.model_dump()
            if request.config_overrides else {}
        )
        await _storage_pg.execute(
            tenant_id,
            """INSERT INTO scrape_jobs (job_id, urls, config_used, status, webhook_url)
               VALUES ($1::uuid, $2::text[], $3::jsonb, $4, $5)""",
            job_id,
            [str(u) for u in request.urls],
            config_json,
            JobStatus.PENDING.value,
            str(request.webhook) if request.webhook else None,
        )

        if _queue is not None:
            _queue.enqueue(
                "orchestrator.tasks.run_scrape_job",
                str(tenant_id),
                job_id,
                job_timeout=_SCRAPE_JOB_TIMEOUT_SECONDS,
            )

    return {
        "job_id": job_id,
        "status": JobStatus.PENDING.value,
        "urls": len(request.urls),
        "tenant": str(tenant_id),
    }


@router.post("/crawl")
async def crawl(
    request: CrawlRequest,
    x_api_key: str = Header(..., alias="X-API-Key"),
) -> dict[str, object]:
    """Enqueue a bulk Scrapy crawl job — for target sets larger than /v1/scrape's
    500-URL cap. Same auth/SSRF/quota checks; runs on the same job queue/worker
    pool via a distinct code path (orchestrator/tasks.py branches on
    config_used["_job_type"])."""
    from api.dependencies import _queue, _storage_pg, _storage_redis, _tenant_resolver
    from core.exceptions import AuthenticationError, SSRFBlockedError
    from core.ssrf_guard import SSRFGuard

    if _tenant_resolver is None:
        raise HTTPException(status_code=503, detail="Service not initialized")
    try:
        tenant_id = await _tenant_resolver.resolve(x_api_key)
    except AuthenticationError:
        raise HTTPException(status_code=401, detail="Invalid API key") from None

    ssrf = SSRFGuard()
    for url_val in request.start_urls:
        try:
            await ssrf.validate(str(url_val))
        except SSRFBlockedError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc

    if _storage_redis is not None and _storage_pg is not None:
        from core.exceptions import QuotaExceededError
        from core.quota import QuotaManager
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
                redis=_storage_redis, daily_limit=daily_limit,
            ).check_and_increment(tenant_id, count=len(request.start_urls))
        except QuotaExceededError:
            raise HTTPException(status_code=429, detail="Daily quota exceeded") from None

    job_id = str(uuid.uuid4())
    if _storage_pg is not None:
        config_json = json.dumps({
            "_job_type": "crawl",
            "spider_name": request.spider_name,
            "start_urls": [str(u) for u in request.start_urls],
        })
        await _storage_pg.execute(
            tenant_id,
            """INSERT INTO scrape_jobs (job_id, urls, config_used, status, webhook_url)
               VALUES ($1::uuid, $2::text[], $3::jsonb, $4, $5)""",
            job_id,
            [str(u) for u in request.start_urls],
            config_json,
            JobStatus.PENDING.value,
            str(request.webhook) if request.webhook else None,
        )

        if _queue is not None:
            _queue.enqueue(
                "orchestrator.tasks.run_scrape_job",
                str(tenant_id),
                job_id,
                job_timeout=_CRAWL_JOB_TIMEOUT_SECONDS,
            )

    return {
        "job_id": job_id,
        "status": JobStatus.PENDING.value,
        "start_urls": len(request.start_urls),
        "tenant": str(tenant_id),
    }


@router.get("/jobs/{job_id}")
async def get_job(
    job_id: str,
    x_api_key: str = Header(..., alias="X-API-Key"),
) -> JobStatusResponse:
    """Get the status and results of a scrape job from the live database."""
    from api.dependencies import _storage_pg, _tenant_resolver
    from core.exceptions import AuthenticationError

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
        "SELECT job_id, status FROM scrape_jobs WHERE job_id = $1::uuid",
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
            fetched_at=r["extracted_at"],
        )
        for r in result_rows
    ]
    errors = [r.error_message for r in results if not r.success and r.error_message]

    progress = (
        1.0 if status in _TERMINAL_STATUSES
        else (0.5 if status == JobStatus.PROCESSING else 0.0)
    )

    return JobStatusResponse(
        # asyncpg returns a uuid.UUID for the UUID column; JobStatusResponse.job_id
        # is typed str, so coerce explicitly (round 16 — this 500'd on every
        # existing job, caught by the full-stack e2e smoke).
        job_id=str(row["job_id"]),
        status=status,
        progress=progress,
        results=results or None,
        error="; ".join(errors) if errors else None,
    )


@router.get("/health")
async def health() -> dict[str, object]:
    """Composite health check — pg/redis/s3 reachability + proxy pool size."""
    from api.dependencies import _storage_pg, _storage_redis, _storage_s3
    from api.health import check_health

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


def register_routes(app: FastAPI) -> None:
    """Register all API routes on the FastAPI app, including /metrics."""
    from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

    @app.get("/metrics")
    async def metrics() -> Response:
        from prometheus_client import REGISTRY

        from api.dependencies import _storage_pg
        from core.tenant import TenantId
        from observability.metrics import count_validated_proxies, proxy_pool_validated_count

        if _storage_pg is not None:
            try:
                tenant = TenantId("system")
                count = await count_validated_proxies(_storage_pg, tenant)
                proxy_pool_validated_count.set(count)
            except Exception:
                logger.warning("proxy_pool_validated_count gauge update failed", exc_info=True)

        return Response(content=generate_latest(REGISTRY), media_type=CONTENT_TYPE_LATEST)

    app.include_router(router)
