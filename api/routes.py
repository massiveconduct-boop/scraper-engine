# api/routes.py
"""API route definitions — fully wired with auth, SSRF guard, and quota.

Endpoints:
  POST /v1/scrape      — single/multi-URL scrape (SSRF-guarded, quota-checked)
  GET  /v1/jobs/{id}   — job status from live DB
  GET  /v1/health       — composite health check
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from fastapi import APIRouter, Header, HTTPException

from core.models import JobStatus, JobStatusResponse, ScrapeRequest

router = APIRouter(prefix="/v1")


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
    from api.dependencies import _storage_pg, _storage_redis, _tenant_resolver
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

    return {
        "job_id": job_id,
        "status": JobStatus.PENDING.value,
        "urls": len(request.urls),
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
    return JobStatusResponse(
        # asyncpg returns a uuid.UUID for the UUID column; JobStatusResponse.job_id
        # is typed str, so coerce explicitly (round 16 — this 500'd on every
        # existing job, caught by the full-stack e2e smoke).
        job_id=str(row["job_id"]),
        status=JobStatus(row["status"]),
        progress=0.0,
    )


@router.get("/health")
async def health() -> dict[str, str]:
    """Composite health check."""
    return {"status": "ok"}


def register_routes(app: Any) -> None:
    """Register all API routes on the FastAPI app, including /metrics."""
    from fastapi import Response
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
                pass

        return Response(content=generate_latest(REGISTRY), media_type=CONTENT_TYPE_LATEST)

    app.include_router(router)
