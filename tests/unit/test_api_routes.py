# tests/unit/test_api_routes.py
"""API route regressions.

get_job UUID coercion: `scrape_jobs.job_id` is a Postgres UUID; asyncpg returns
it as a `uuid.UUID`, but `JobStatusResponse.job_id` is typed `str`. The route
must `str()` it or Pydantic raises and the endpoint 500s on every existing job
(round 16 — caught by the full-stack e2e smoke).
"""

import uuid
from unittest.mock import AsyncMock

import pytest

import api.dependencies as deps
from api.routes import get_job
from core.models import JobStatus


@pytest.fixture
def wired_deps(monkeypatch):
    """Wire module-level deps get_job reads: a resolver and a PG returning a row
    whose job_id is a real uuid.UUID (as asyncpg does)."""
    resolver = AsyncMock()
    resolver.resolve.return_value = "system"
    monkeypatch.setattr(deps, "_tenant_resolver", resolver)

    pg = AsyncMock()
    monkeypatch.setattr(deps, "_storage_pg", pg)
    return pg


@pytest.mark.asyncio
async def test_get_job_coerces_uuid_job_id_to_str(wired_deps):
    jid = uuid.uuid4()
    wired_deps.fetch.return_value = [{"job_id": jid, "status": "PENDING"}]

    resp = await get_job(str(jid), x_api_key="sk-admin")

    # the bug: passing the raw UUID would raise pydantic ValidationError (500).
    assert resp.job_id == str(jid)
    assert isinstance(resp.job_id, str)
    assert resp.status == JobStatus.PENDING


@pytest.mark.asyncio
async def test_get_job_missing_row_404(wired_deps):
    from fastapi import HTTPException

    wired_deps.fetch.return_value = []
    with pytest.raises(HTTPException) as ei:
        await get_job(str(uuid.uuid4()), x_api_key="sk-admin")
    assert ei.value.status_code == 404
