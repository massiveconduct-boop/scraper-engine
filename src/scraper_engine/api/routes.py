# api/routes.py
"""API route definitions — fully wired with auth, SSRF guard, and quota.

Endpoints:
  POST   /v1/scrape        — single/multi-URL scrape (SSRF-guarded, quota-checked)
  POST   /v1/crawl         — bulk Scrapy crawl for target sets >500 URLs
  GET    /v1/jobs          — list jobs for the calling tenant (round 56)
  GET    /v1/jobs/{id}     — job status from live DB
  GET    /v1/jobs/{id}/dlq — raw dead-letter detail for a job
  DELETE /v1/jobs/{id}     — cancel a PENDING/PROCESSING job
  GET    /v1/dlq           — tenant-wide dead-letter listing (round 56)
  GET    /v1/quota         — remaining daily quota for the calling tenant (round 56)
  GET    /v1/webhook-events — webhook event taxonomy + payload schema (round 56)
  GET    /v1/health        — composite health check
"""

from __future__ import annotations

import functools
import json
import logging
import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, FastAPI, Header, HTTPException, Response

from scraper_engine.config.schema import AppConfig, HostCapacityConfig
from scraper_engine.core.models import (
    CrawlRequest,
    DeadLetterEntryResponse,
    FailureCategory,
    FetchResult,
    JobStatus,
    JobStatusResponse,
    JobSummaryResponse,
    ScrapeRequest,
)

router = APIRouter(prefix="/v1")
logger = logging.getLogger(__name__)

_TERMINAL_STATUSES = frozenset(
    {JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED, JobStatus.DEAD_LETTER}
)

_SCRAPE_JOB_TIMEOUT_SECONDS = 600
_CRAWL_JOB_TIMEOUT_SECONDS = 1800  # bulk crawls run longer than a bounded scrape job

# Round 42 — a flat 600s ceiling silently killed real batches once they grew
# past roughly 15-20 URLs. Live-caught: a real 51-URL research_agent job
# (free-pool proxies, L1->L2->L3 escalation, orchestrator/worker.py's
# per-URL loop runs sequentially, not concurrently) was still only 21/51
# through at the 10-minute mark (~29s/URL observed) when RQ's own hard job
# timeout force-killed the process mid-run. Because the kill is a hard
# termination (RQ's own watchdog, not a Python exception the app code can
# catch), _run_scrape_job never got to run its own failure-path cleanup —
# the scrape_jobs.status row was left stuck at PROCESSING forever even
# though RQ's own registry correctly recorded the job as failed. Scaling
# the timeout by URL count (floor at the historical 600s, so small jobs are
# unaffected) closes the root cause. Round 46 — round 42's 60s/URL still
# wasn't enough: a real 51-URL job hit the resulting 3060s ceiling and got
# hard-killed again mid-run. 60s/URL didn't actually cover a single URL's
# own worst case — L1+L2+L3's own per-level timeouts alone sum to 120s
# (base.yaml: 20+40+60), before counting retries
# (orchestrator/worker.py's _SAME_LEVEL_PROXY_RETRIES) at all. Bumped to
# 120s/URL, matching that real sum instead of a smaller number that never
# actually bounded the worst case it was meant to cover.
# Round 63 — raised from 120. That figure was the sum of the three levels'
# OWN timeouts (base.yaml: 20+40+60), i.e. a URL's worst case when it is the
# only thing running. Since round 49 a job dispatches URLs concurrently, and
# a URL's WALL time now also includes waiting for a politeness slot behind
# its siblings — measured on a real 10-URL single-domain Jumia job:
# `slot_wait_ms` up to 88s and a worst per-URL `total_ms` of 156s, against a
# 120s allowance. The job was killed by this timeout at 1303s having
# successfully fetched 9 of 10 URLs, which then surfaced to the caller as a
# FAILED job rather than a slow one. 180s covers the measured worst case
# with margin. The real diagnosis is in each result's `timings` now, so a
# job that trips even this is answerable rather than mysterious.
_PER_URL_TIMEOUT_SECONDS = 180


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


_MAX_PAGE_LIMIT = 500


def _validate_pagination(limit: int, offset: int) -> None:
    """Raise 422 on out-of-range limit/offset (round 56). Deliberately plain
    int params with manual validation rather than FastAPI's `Query(...)`
    marker — every test in this module calls route functions directly
    (bypassing FastAPI's DI), and a `Query(...)` default is never resolved
    to its plain value outside that DI path (same class of gotcha already
    noted on ScrapeRequest's idempotency_key Header() default above)."""
    if not (1 <= limit <= _MAX_PAGE_LIMIT):
        raise HTTPException(
            status_code=422, detail=f"limit must be between 1 and {_MAX_PAGE_LIMIT}"
        )
    if offset < 0:
        raise HTTPException(status_code=422, detail="offset must be >= 0")


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
    # Round 64 — storage, Redis (quota) and the queue are required, not
    # optional: with any of them missing this used to answer 200 with a job
    # id it never saved, never charged, or never queued.
    if _ssrf_guard is None or _storage_pg is None or _storage_redis is None or _queue is None:
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
    if idempotency_key:
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

    # Round 54 — this INSERT and the enqueue below are two separate
    # operations, not one transaction. Before this fix, a transient
    # Redis blip here (or any other exception from .enqueue()) left
    # the row already committed as PENDING with no corresponding rq
    # job ever created — invisible to stuck_job_reaper (which only
    # ever looked at PROCESSING) and to the caller, who'd just see a
    # 500 and have no way to know whether the job existed. Live-
    # found: 17 real research_agent jobs stuck at PENDING for days,
    # none with a matching rq:job:* Redis key. Fail loud and clean
    # instead: mark the row FAILED so it's not silently orphaned,
    # and tell the caller plainly that nothing was queued.
    try:
        _queue.enqueue(
            "scraper_engine.orchestrator.tasks.run_scrape_job",
            str(tenant_id),
            job_id,
            job_id=job_id,
            job_timeout=max(_SCRAPE_JOB_TIMEOUT_SECONDS, valid_count * _PER_URL_TIMEOUT_SECONDS),
        )
    except Exception:
        logger.exception("scrape_job_enqueue_failed job_id=%s tenant=%s", job_id, tenant_id)
        await _storage_pg.execute(
            tenant_id,
            "UPDATE scrape_jobs SET status = $1, updated_at = NOW() WHERE job_id = $2::uuid",
            JobStatus.FAILED.value,
            job_id,
        )
        raise HTTPException(
            status_code=503,
            detail="Failed to enqueue job — no work was queued, safe to retry",
        ) from None

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
    # Round 64 — storage, Redis (quota) and the queue are required, not
    # optional: with any of them missing this used to answer 200 with a job
    # id it never saved, never charged, or never queued.
    if _ssrf_guard is None or _storage_pg is None or _storage_redis is None or _queue is None:
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
    if idempotency_key:
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
    # (level_used=0 — blocked before any level/spider ever ran).
    #
    # SSRFGuard raises SSRFBlockedError for two different situations — a
    # real block and an unresolvable/dead domain (see
    # exceptions.py::SSRFBlockedError) — so the category must follow
    # `is_unresolvable`, same as fetcher/_failure.py's
    # classify_fetch_exception, rather than hardcoding SSRF_BLOCKED for
    # both.
    for blocked_exc in blocked:
        category = (
            FailureCategory.HOST_UNREACHABLE
            if blocked_exc.is_unresolvable
            else FailureCategory.SSRF_BLOCKED
        )
        await _storage_pg.execute(
            tenant_id,
            """INSERT INTO scrape_results
                   (job_id, url, success, level_used, error_message, failure_category)
               VALUES ($1::uuid, $2, FALSE, 0, $3, $4)""",
            job_id,
            blocked_exc.url,
            str(blocked_exc),
            category.value,
        )

    # Round 54 — same fix as /v1/scrape above: enqueue isn't atomic
    # with the INSERT above it, so a transient Redis error here must
    # not leave this row permanently PENDING with nothing queued.
    try:
        _queue.enqueue(
            "scraper_engine.orchestrator.tasks.run_scrape_job",
            str(tenant_id),
            job_id,
            job_id=job_id,
            job_timeout=_CRAWL_JOB_TIMEOUT_SECONDS,
        )
    except Exception:
        logger.exception("crawl_job_enqueue_failed job_id=%s tenant=%s", job_id, tenant_id)
        await _storage_pg.execute(
            tenant_id,
            "UPDATE scrape_jobs SET status = $1, updated_at = NOW() WHERE job_id = $2::uuid",
            JobStatus.FAILED.value,
            job_id,
        )
        raise HTTPException(
            status_code=503,
            detail="Failed to enqueue job — no work was queued, safe to retry",
        ) from None

    return {
        "job_id": job_id,
        "status": JobStatus.PENDING.value,
        "start_urls": len(valid_start_urls),
        "blocked_urls": len(blocked),
        "tenant": str(tenant_id),
    }


@router.get("/jobs")
async def list_jobs(
    x_api_key: str = Header(..., alias="X-API-Key"),
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, object]:
    """List jobs for the calling tenant (round 56) — schema-per-tenant search_path
    (see PostgresClient.acquire) already scopes this to the caller's own jobs,
    same as GET /v1/jobs/{job_id}. `limit` caps at 500, matching ScrapeRequest's
    existing per-request URL cap, so one tenant can't force an unbounded scan."""
    from scraper_engine.api.dependencies import _storage_pg, _tenant_resolver
    from scraper_engine.core.exceptions import AuthenticationError

    _validate_pagination(limit, offset)
    if status is not None:
        try:
            JobStatus(status)
        except ValueError:
            raise HTTPException(status_code=422, detail=f"Invalid status: '{status}'") from None

    if _tenant_resolver is None:
        raise HTTPException(status_code=503, detail="Service not initialized")
    try:
        tenant_id = await _tenant_resolver.resolve(x_api_key)
    except AuthenticationError:
        raise HTTPException(status_code=401, detail="Invalid API key") from None

    if _storage_pg is None:
        return {"jobs": [], "limit": limit, "offset": offset, "count": 0}

    rows = await _storage_pg.fetch(
        tenant_id,
        """SELECT job_id, status, urls, created_at, updated_at FROM scrape_jobs
           WHERE ($1::text IS NULL OR status = $1)
           ORDER BY created_at DESC LIMIT $2 OFFSET $3""",
        status,
        limit,
        offset,
    )
    jobs = [
        JobSummaryResponse(
            job_id=str(r["job_id"]),
            status=JobStatus(r["status"]),
            url_count=len(r["urls"]),
            created_at=r["created_at"],
            updated_at=r["updated_at"],
        )
        for r in rows
    ]
    return {"jobs": jobs, "limit": limit, "offset": offset, "count": len(jobs)}


def _split_timings_column(
    raw: str | None,
) -> tuple[dict[str, int] | None, list[dict[str, Any]] | None]:
    """Undo orchestrator/tasks.py::_timings_column — the stored JSONB holds the
    integer phase timings plus an optional `escalations` list (round 64)."""
    if not raw:
        return None, None
    payload = json.loads(raw)
    escalations = payload.pop("escalations", None)
    return (payload or None), escalations


@router.get("/jobs/{job_id}")
async def get_job(
    job_id: str,
    x_api_key: str = Header(..., alias="X-API-Key"),
    # Only return results extracted strictly after this timestamp (ISO-8601).
    # A plain annotated default rather than FastAPI's Query(...) marker, for
    # the reason _validate_pagination's docstring gives: this module's tests
    # call the route functions directly, where a Query(...) default is never
    # resolved to its plain value. FastAPI still exposes a bare scalar
    # parameter as a query parameter.
    since: datetime | None = None,
) -> JobStatusResponse:
    """Get the status and results of a scrape job from the live database.

    Results have always been readable while a job is still PROCESSING — rows
    land one per URL as each completes (orchestrator/tasks.py's on_result
    callback), and `progress` is computed from how many have landed. What was
    missing until round 63 was a cursor: every poll of a 95-URL job re-sent
    every result already seen, so callers avoided multi-URL jobs entirely.
    `since` is that cursor.
    """
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
        "SELECT job_id, status, urls, created_at, started_at, finished_at "
        "FROM scrape_jobs WHERE job_id = $1::uuid",
        job_id,
    )
    if not rows:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")

    row = rows[0]
    status = JobStatus(row["status"])

    # progress must count the whole job even when the caller is paging with
    # `since`, so the count is its own query rather than len(result_rows).
    completed_rows = await _storage_pg.fetch(
        tenant_id,
        "SELECT COUNT(*) AS n FROM scrape_results WHERE job_id = $1::uuid",
        job_id,
    )
    completed = int(completed_rows[0]["n"]) if completed_rows else 0

    result_rows = await _storage_pg.fetch(
        tenant_id,
        """SELECT url, success, http_status, proxy_source, is_challenge_page, level_used,
                  proxy_used, markdown, json_data, network_events, html_snapshot_url,
                  time_taken_ms, error_message, failure_category, extracted_at, timings
           FROM scrape_results
           WHERE job_id = $1::uuid AND ($2::timestamptz IS NULL OR extracted_at > $2::timestamptz)
           ORDER BY extracted_at""",
        job_id,
        since,
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
            network_events=json.loads(r["network_events"]) if r["network_events"] else None,
            duration_ms=r["time_taken_ms"] or 0,
            error_message=r["error_message"],
            failure_category=(
                FailureCategory(r["failure_category"]) if r["failure_category"] else None
            ),
            html_snapshot_url=r["html_snapshot_url"],
            fetched_at=r["extracted_at"],
            # proxy_source and timings were both persisted but dropped on the
            # way back out, so a caller could not see which proxy path served
            # a URL or where its time went (round 63).
            proxy_source=r["proxy_source"],
            timings=timings,
            escalations=escalations,
        )
        for r in result_rows
        for timings, escalations in [_split_timings_column(r["timings"])]
    ]
    errors = [r.error_message for r in results if not r.success and r.error_message]

    # Real per-URL progress (round 29) — results now persist incrementally
    # as each URL completes (see orchestrator/tasks.py's on_result
    # callback), so len(result_rows) genuinely reflects how many of the
    # job's URLs are done, not a hardcoded stand-in.
    total_urls = len(row["urls"]) or 1
    progress = 1.0 if status in _TERMINAL_STATUSES else min(1.0, completed / total_urls)
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
        queued_ms=_elapsed_ms(row["created_at"], row["started_at"]),
        runtime_ms=_elapsed_ms(row["started_at"], row["finished_at"]),
    )


def _elapsed_ms(start: datetime | None, end: datetime | None) -> int | None:
    """Milliseconds between two job-phase timestamps, or None if either has
    not happened yet (round 63)."""
    if start is None or end is None:
        return None
    return int((end - start).total_seconds() * 1000)


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


@router.get("/dlq")
async def list_dlq(
    x_api_key: str = Header(..., alias="X-API-Key"),
    limit: int = 100,
    offset: int = 0,
) -> list[DeadLetterEntryResponse]:
    """Tenant-wide dead-letter listing (round 56) — DeadLetterQueue.list_for_tenant
    already supports a `job_id=None` "everything currently dead for this tenant"
    mode (used internally by ops tooling and the dlq_size Prometheus gauge); this
    route is the caller-facing sibling of GET /v1/jobs/{job_id}/dlq, which only
    covers one job at a time and requires already knowing its id."""
    from scraper_engine.api.dependencies import _storage_pg, _tenant_resolver
    from scraper_engine.core.exceptions import AuthenticationError
    from scraper_engine.storage.dlq import DeadLetterQueue

    _validate_pagination(limit, offset)

    if _tenant_resolver is None:
        raise HTTPException(status_code=503, detail="Service not initialized")
    try:
        tenant_id = await _tenant_resolver.resolve(x_api_key)
    except AuthenticationError:
        raise HTTPException(status_code=401, detail="Invalid API key") from None

    if _storage_pg is None:
        return []

    entries = await DeadLetterQueue(_storage_pg).list_for_tenant(
        tenant_id, limit=limit, offset=offset
    )
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


@router.get("/quota")
async def get_quota(
    x_api_key: str = Header(..., alias="X-API-Key"),
) -> dict[str, object]:
    """Remaining daily quota for the calling tenant (round 56) — QuotaManager
    already tracks this (core/quota.py), it was just never exposed; callers
    previously only found out their limit by hitting a 429. Same daily-limit
    lookup query and same None-falls-back-to-DEFAULT_DAILY_LIMIT behavior as
    POST /v1/scrape's quota check."""
    from scraper_engine.api.dependencies import _storage_pg, _storage_redis, _tenant_resolver
    from scraper_engine.core.exceptions import AuthenticationError
    from scraper_engine.core.quota import QuotaManager, seconds_until_quota_reset

    if _tenant_resolver is None:
        raise HTTPException(status_code=503, detail="Service not initialized")
    try:
        tenant_id = await _tenant_resolver.resolve(x_api_key)
    except AuthenticationError:
        raise HTTPException(status_code=401, detail="Invalid API key") from None

    if _storage_pg is None or _storage_redis is None:
        raise HTTPException(status_code=503, detail="Service not initialized")

    daily_limit = None
    row = await _storage_pg.fetchrow(
        tenant_id,
        "SELECT quota_daily_limit FROM public.tenants WHERE tenant_id = $1",
        str(tenant_id),
    )
    if row is not None:
        daily_limit = row["quota_daily_limit"]

    effective_limit = daily_limit or QuotaManager.DEFAULT_DAILY_LIMIT
    manager = QuotaManager(redis=_storage_redis, daily_limit=daily_limit)
    used = await manager.current_usage(tenant_id)
    remaining = await manager.remaining(tenant_id)
    return {
        "tenant": str(tenant_id),
        "daily_limit": effective_limit,
        "used": used,
        "remaining": remaining,
        "resets_in_seconds": seconds_until_quota_reset(),
    }


@router.get("/webhook-events")
async def list_webhook_events(
    x_api_key: str = Header(..., alias="X-API-Key"),
) -> dict[str, object]:
    """Webhook event taxonomy + payload schema (round 56) — a caller wiring up
    a webhook receiver had no way to discover WebhookEventType's values or
    WebhookEvent's shape (orchestrator/webhook_events.py) short of reading this
    repo's source. Pure static reflection, no DB/Redis touch — same auth
    posture as every other /v1 route, but nothing tenant-specific to fail on."""
    from scraper_engine.api.dependencies import _tenant_resolver
    from scraper_engine.core.exceptions import AuthenticationError
    from scraper_engine.orchestrator.webhook_events import WebhookEvent, WebhookEventType

    if _tenant_resolver is None:
        raise HTTPException(status_code=503, detail="Service not initialized")
    try:
        await _tenant_resolver.resolve(x_api_key)
    except AuthenticationError:
        raise HTTPException(status_code=401, detail="Invalid API key") from None

    return {
        "event_types": [e.value for e in WebhookEventType],
        "payload_schema": WebhookEvent.model_json_schema(),
    }


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


@functools.cache
def _host_capacity_config() -> HostCapacityConfig:
    """Loaded once: /v1/health is polled every 10s by the compose healthcheck."""
    from scraper_engine.config.loader import load_config

    return load_config().host_capacity


@router.get("/health")
async def health() -> dict[str, object]:
    """Composite health check — pg/redis/s3 reachability + daemon liveness + proxy pool size."""
    from scraper_engine.api.dependencies import _storage_pg, _storage_redis, _storage_s3
    from scraper_engine.api.health import check_health

    if _storage_pg is None or _storage_redis is None:
        raise HTTPException(status_code=503, detail="Service not initialized")

    status = await check_health(
        _storage_pg, _storage_redis, _storage_s3, _host_capacity_config()
    )
    payload: dict[str, object] = {
        "status": "ok" if status.healthy else "degraded",
        "pgbouncer_reachable": status.pgbouncer_reachable,
        "redis_reachable": status.redis_reachable,
        "s3_reachable": status.s3_reachable,
        "proxy_pool_size": status.proxy_pool_size,
        "daemons": status.daemons,
        "checks": status.checks,
    }
    if status.browser_capacity is not None:
        payload["browser_capacity"] = status.browser_capacity
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
            from scraper_engine.core.host_identity import resolve_host_id
            from scraper_engine.core.tenant import TenantId
            from scraper_engine.observability.metrics import (
                count_validated_proxies,
                proxy_pool_validated_count,
                refresh_capsolver_spend,
                refresh_dlq_size,
                refresh_host_capacity,
                refresh_proxy_source_health,
                refresh_redis_backed_counters,
            )
            from scraper_engine.orchestrator.host_capacity import HostAdmission

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
                host_cfg = _host_capacity_config()
                if host_cfg.enabled:
                    try:
                        await refresh_host_capacity(
                            redis, HostAdmission(redis.raw, resolve_host_id(), host_cfg)
                        )
                    except Exception:
                        logger.warning("host capacity gauge update failed", exc_info=True)

            return Response(content=generate_latest(REGISTRY), media_type=CONTENT_TYPE_LATEST)

    app.include_router(router)
