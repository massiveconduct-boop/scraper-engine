# Per-Tenant Quota Enforcement — Closure Evidence

## 1. Signature Verification

```
$ grep -A 25 "def check_and_increment" core/quota.py

    async def check_and_increment(self, tenant_id: TenantId, count: int = 1) -> None:
        """Atomically check quota and increment. Raises QuotaExceededError if limit hit."""
        result = await self._redis.eval(
            """
            local key = KEYS[1]
            local limit = tonumber(ARGV[1])
            local count = tonumber(ARGV[2])
            local ttl = tonumber(ARGV[3])
            local current = tonumber(redis.call('GET', key) or '0')
            if current + count > limit then
                return -1
            end
            local new_val = redis.call('INCRBY', key, count)
            redis.call('EXPIRE', key, ttl)
            return new_val
            """,
            1,
            self._quota_key(tenant_id),
            self._limit,
            count,
            86400 * 2,
        )
        if result == -1:
            raise QuotaExceededError(tenant_id=str(tenant_id), limit=self._limit)

    async def current_usage(self, tenant_id: TenantId) -> int:
```

Raises `QuotaExceededError`, never returns bool. `__init__` takes `redis` + optional `daily_limit` with `DEFAULT_DAILY_LIMIT = 10_000` fallback. The `tenants.quota_daily_limit` column existed but was never read — dead column. Now wired.

## 2. Code Fix — `api/routes.py` (complete, no `daily_limit=3`)

```python
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
        )
    return value


@router.post("/scrape")
async def scrape(
    request: ScrapeRequest,
    x_api_key: str = Header(..., alias="X-API-Key"),
) -> dict[str, object]:
    """Enqueue a scrape job with SSRF validation, tenant auth, and quota check."""
    from api.auth import TenantResolver
    from api.dependencies import _tenant_resolver, _storage_pg, _storage_redis, _worker
    from core.exceptions import AuthenticationError, SSRFBlockedError
    from core.ssrf_guard import SSRFGuard

    # ── Tenant resolution ──
    if _tenant_resolver is None:
        raise HTTPException(status_code=503, detail="Service not initialized")

    try:
        tenant_id = await _tenant_resolver.resolve(x_api_key)
    except AuthenticationError:
        raise HTTPException(status_code=401, detail="Invalid API key")

    # ── SSRF validation on every URL ──
    ssrf = SSRFGuard()
    for url_val in request.urls:
        try:
            await ssrf.validate(str(url_val))
        except SSRFBlockedError as exc:
            raise HTTPException(status_code=403, detail=str(exc))

    # ── Quota enforcement ──
    # Per-tenant limit from tenants.quota_daily_limit, falling back to
    # QuotaManager.DEFAULT_DAILY_LIMIT (10_000) when no row exists.
    # Raises QuotaExceededError if limit hit → 429.
    # No bare except — if QuotaManager breaks, it surfaces as a 500.
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
            raise HTTPException(status_code=429, detail="Daily quota exceeded")

    # ── Persist job ──
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
    from api.dependencies import _tenant_resolver, _storage_pg
    from core.exceptions import AuthenticationError

    _validate_uuid(job_id)

    if _tenant_resolver is None:
        raise HTTPException(status_code=503, detail="Service not initialized")

    try:
        tenant_id = await _tenant_resolver.resolve(x_api_key)
    except AuthenticationError:
        raise HTTPException(status_code=401, detail="Invalid API key")

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
        job_id=row["job_id"],
        status=JobStatus(row["status"]),
        progress=0.0,
    )


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


def register_routes(app: Any) -> None:
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
```

**Changes:** `daily_limit=3` removed. Per-tenant limit from `public.tenants.quota_daily_limit` with `DEFAULT_DAILY_LIMIT = 10_000` fallback. Only `QuotaExceededError` caught → 429. No bare except.

## 3. Raw Curl Transcripts — Per-Tenant Enforcement

### Seeds (17:52:02Z)

```
$ docker exec scraper_engine-postgres-1 psql -U scraper -d scraper_engine \
  -c "SELECT tenant_id, quota_daily_limit FROM tenants ORDER BY tenant_id;"
 tenant_id | quota_daily_limit
-----------+-------------------
 other     |                 5
 system    |                 2
```

Redis flushed before each tenant block.

### system (limit=2) — 200, 200, 429

```
$ docker exec scraper_engine-redis-1 redis-cli FLUSHALL
OK

$ curl -s -o /dev/null -w "%{http_code}\n" -X POST http://localhost:8000/v1/scrape -H "X-API-Key: sk-admin" -H "Content-Type: application/json" -d '{"urls":["http://example.com"]}'
200

$ curl -s -o /dev/null -w "%{http_code}\n" -X POST http://localhost:8000/v1/scrape -H "X-API-Key: sk-admin" -H "Content-Type: application/json" -d '{"urls":["http://example.com"]}'
200

$ curl -s -o /dev/null -w "%{http_code}\n" -X POST http://localhost:8000/v1/scrape -H "X-API-Key: sk-admin" -H "Content-Type: application/json" -d '{"urls":["http://example.com"]}'
429
```

### other (limit=5) — 200 ×5, 429

```
$ docker exec scraper_engine-redis-1 redis-cli FLUSHALL
OK

$ curl -s -o /dev/null -w "%{http_code}\n" -X POST http://localhost:8000/v1/scrape -H "X-API-Key: sk-other" -H "Content-Type: application/json" -d '{"urls":["http://example.com"]}'
200

$ curl -s -o /dev/null -w "%{http_code}\n" -X POST http://localhost:8000/v1/scrape -H "X-API-Key: sk-other" -H "Content-Type: application/json" -d '{"urls":["http://example.com"]}'
200

$ curl -s -o /dev/null -w "%{http_code}\n" -X POST http://localhost:8000/v1/scrape -H "X-API-Key: sk-other" -H "Content-Type: application/json" -d '{"urls":["http://example.com"]}'
200

$ curl -s -o /dev/null -w "%{http_code}\n" -X POST http://localhost:8000/v1/scrape -H "X-API-Key: sk-other" -H "Content-Type: application/json" -d '{"urls":["http://example.com"]}'
200

$ curl -s -o /dev/null -w "%{http_code}\n" -X POST http://localhost:8000/v1/scrape -H "X-API-Key: sk-other" -H "Content-Type: application/json" -d '{"urls":["http://example.com"]}'
200

$ curl -s -o /dev/null -w "%{http_code}\n" -X POST http://localhost:8000/v1/scrape -H "X-API-Key: sk-other" -H "Content-Type: application/json" -d '{"urls":["http://example.com"]}'
429
```

Two tenants, distinct per-tenant limits, independent enforcement. `tenants.quota_daily_limit` column now live.

## 4. Quota Restoration

```
$ docker exec scraper_engine-redis-1 redis-cli FLUSHALL
OK

$ docker exec scraper_engine-postgres-1 psql -U scraper -d scraper_engine \
  -c "UPDATE tenants SET quota_daily_limit = 10000 WHERE tenant_id IN ('system', 'other');"
UPDATE 2

$ docker exec scraper_engine-postgres-1 psql -U scraper -d scraper_engine \
  -c "SELECT tenant_id, quota_daily_limit FROM tenants ORDER BY tenant_id;"
 tenant_id | quota_daily_limit
-----------+-------------------
 other     |             10000
 system    |             10000
(2 rows)
```

Restored: `quota_daily_limit = 10000` for both tenants. Redis cleared of all test keys.
