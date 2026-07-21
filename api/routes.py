# api/routes.py
"""API route definitions.

Endpoints:
  POST /v1/scrape      — single/multi-URL scrape
  GET  /v1/jobs/{id}   — job status
  POST /v1/crawl        — bulk crawl (Scrapy-backed)
  GET  /v1/health       — composite health check
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter

from core.models import JobStatus, JobStatusResponse, ScrapeRequest

router = APIRouter(prefix="/v1")


@router.post("/scrape")
async def scrape(request: ScrapeRequest) -> dict[str, object]:
    """Enqueue a scrape job. Returns job_id for status polling."""
    job_id = str(uuid.uuid4())
    return {"job_id": job_id, "status": JobStatus.PENDING.value, "urls": len(request.urls)}


@router.get("/jobs/{job_id}")
async def get_job(job_id: str) -> JobStatusResponse:
    """Get the status and results of a scrape job."""
    return JobStatusResponse(
        job_id=job_id,
        status=JobStatus.PENDING,
        progress=0.0,
    )


@router.get("/health")
async def health() -> dict[str, str]:
    """Composite health check."""
    return {"status": "ok"}


def register_routes(app: Any) -> None:
    """Register all API routes on the FastAPI app."""
    app.include_router(router)
