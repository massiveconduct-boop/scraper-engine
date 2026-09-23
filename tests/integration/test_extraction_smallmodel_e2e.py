# tests/integration/test_extraction_smallmodel_e2e.py
"""Queue-level integration test: a full job with extraction_enable_smallmodel=True,
end-to-end through orchestrator/tasks.py, against real Postgres+Redis+MinIO —
closes .wolf/STATUS.md's open item: "No queue-level integration test asserts
a full job with extraction_enable_smallmodel=True end-to-end."

Uses an in-process fake extraction-engine HTTP server (same pattern as
proxy/judge_server.py: stdlib ThreadingHTTPServer on port=0) instead of
docker-in-CI or a real deployed extraction-engine service — avoids both the
CI complexity and the flakiness a real network dependency would add.

The real network fetch itself (Worker._fetch_url) is monkeypatched, matching
tests/integration/test_worker_escalation.py's existing convention. This is
not a shortcut around the interesting part of the pipeline — it's respect
for core/ssrf_guard.py's hard-denied private/loopback ranges (SSRF is a
non-negotiable invariant per CLAUDE.md; a test has no special exemption to
point a real fetch at 127.0.0.1). Everything downstream of the fetch is
real: the scrape_jobs row, tasks.py's job lifecycle (status transitions,
webhook-skip path), the real HTTP call to the fake extraction engine,
extraction_engine_client.py's schema-wrapping, and the scrape_results
Postgres row (including the S3 HTML snapshot write).
"""

from __future__ import annotations

import json
import os
import threading
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from scraper_engine.core.models import FetchResult, JobStatus
from scraper_engine.core.tenant import TenantId
from scraper_engine.orchestrator import tasks
from scraper_engine.orchestrator.worker import Worker
from scraper_engine.storage.postgres_client import PostgresClient

EXTRACTED_MARKER = {"title": "extraction-engine-e2e-marker"}


class _FakeExtractHandler(BaseHTTPRequestHandler):
    """Stands in for a real extraction-engine instance's POST /v1/extract."""

    def do_POST(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler's naming
        length = int(self.headers.get("Content-Length", 0))
        payload = json.loads(self.rfile.read(length))
        # Asserting on the request shape here (not just returning a canned
        # response) is what actually proves extraction_enable_smallmodel
        # reached the wire — a test that only checks the persisted result
        # couldn't tell a real smallmodel request apart from one where the
        # flag was silently dropped somewhere in the pipeline.
        assert payload["enable_smallmodel"] is True
        assert payload["enable_llm"] is False
        assert payload["schema"] == {
            "schema_version": "1.0.0",
            "fields": {"title": "string"},
        }
        body = json.dumps(EXTRACTED_MARKER).encode()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        pass


@pytest.fixture
def fake_extraction_engine():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeExtractHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    yield f"http://127.0.0.1:{port}"
    server.shutdown()
    server.server_close()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_extraction_smallmodel_end_to_end(monkeypatch, fake_extraction_engine) -> None:
    monkeypatch.setenv("EXTRACTION_ENGINE_BASE_URL", fake_extraction_engine)
    # tasks.py's per-job load_config() call reads these fresh from the
    # environment — base.yaml's own defaults are docker-network hostnames
    # (pgbouncer/redis/minio) that only resolve inside docker-compose's
    # network, not from a host-side pytest process. CI's `integration`/
    # `chaos` jobs (.github/workflows/test.yml) always map Postgres/Redis
    # to the plain default ports, so that's the default here too;
    # POSTGRES_PORT/REDIS_PORT stay overridable for a local dev box where
    # those defaults collide with something else already listening there
    # (docker-compose.yml's own ${VAR:-default} convention, see CLAUDE.md).
    postgres_port = os.environ.get("POSTGRES_PORT", "5432")
    redis_port = os.environ.get("REDIS_PORT", "6379")
    monkeypatch.setenv(
        "DATABASE_URL", f"postgresql://scraper:scraper@localhost:{postgres_port}/scraper_engine"
    )
    monkeypatch.setenv("REDIS_URL", f"redis://localhost:{redis_port}/0")
    monkeypatch.setenv("S3_ENDPOINT", "http://localhost:9000")

    async def _fake_fetch_url(
        self: Worker,
        tenant_id: TenantId,
        url: str,
        level: int,
        overrides: object = None,
        force_gateway: bool = False,
        skip_botasaurus: bool = False,
        admission: object = None,
    ) -> FetchResult:
        assert level == 1, "this test's fake fetch only succeeds at L1 — escalation must stop there"
        return FetchResult(
            url=url,
            success=True,
            html="<html><body><h1>extraction e2e fixture page</h1></body></html>",
            level_used=1,
            duration_ms=5,
        )

    monkeypatch.setattr(Worker, "_fetch_url", _fake_fetch_url)

    # BrowserPool.start()/shutdown() run unconditionally around every job
    # (tasks.py::_run_scrape) regardless of whether L2/L3 ever get used —
    # the fake L1 fetch above guarantees they don't here, so real Camoufox
    # launches would only add cost with zero coverage value for this test.
    async def _noop(self: object) -> None:
        return None

    monkeypatch.setattr("scraper_engine.browser.pool.BrowserPool.start", _noop)
    monkeypatch.setattr("scraper_engine.browser.pool.BrowserPool.shutdown", _noop)

    tenant = TenantId("system")
    job_id = str(uuid.uuid4())
    domain = f"{uuid.uuid4().hex[:12]}.extraction-e2e.invalid"
    url = f"https://{domain}/"

    pg = PostgresClient(
        pgbouncer_dsn=f"postgresql://scraper:scraper@localhost:{postgres_port}/scraper_engine",
        pool_size=5,
    )
    await pg.start()

    config_used = {
        "extraction_schema": {"title": "string"},
        "extraction_enable_smallmodel": True,
    }

    try:
        await pg.execute(
            tenant,
            """INSERT INTO scrape_jobs
                   (job_id, urls, config_used, status, webhook_url, idempotency_key)
               VALUES ($1::uuid, $2::text[], $3::jsonb, $4, $5, $6)""",
            job_id,
            [url],
            json.dumps(config_used),
            JobStatus.PENDING.value,
            None,
            None,
        )

        # The exact function rq resolves and calls (tasks.py's module
        # docstring) — _run_scrape_job is its async body, called directly
        # rather than via tasks.run_scrape_job's asyncio.run() wrapper
        # (which would try to start a second event loop inside pytest-
        # asyncio's already-running one) or a live rq dequeue loop (real
        # rq worker mechanics are the library's own tested code, not
        # ours — spinning one up here would add process-management
        # flakiness without adding real coverage of this project's code).
        await tasks._run_scrape_job(str(tenant), job_id)

        job_row = await pg.fetchrow(
            tenant, "SELECT status FROM scrape_jobs WHERE job_id = $1::uuid", job_id
        )
        assert job_row is not None
        assert job_row["status"] == JobStatus.COMPLETED.value

        result_row = await pg.fetchrow(
            tenant,
            "SELECT success, level_used, json_data FROM scrape_results "
            "WHERE job_id = $1::uuid AND url = $2",
            job_id,
            url,
        )
        assert result_row is not None, "no scrape_results row persisted for this job/url"
        assert result_row["success"] is True
        assert result_row["level_used"] == 1

        json_data = result_row["json_data"]
        extracted = json.loads(json_data) if isinstance(json_data, str) else json_data
        assert extracted == EXTRACTED_MARKER, (
            "persisted json_data must be the fake extraction-engine's response, "
            "not an AdaptiveSelector fallback — proves extraction_enable_smallmodel "
            "actually routed through the real extraction-engine client end to end"
        )
    finally:
        await pg.execute(tenant, "DELETE FROM scrape_results WHERE job_id = $1::uuid", job_id)
        await pg.execute(tenant, "DELETE FROM scrape_jobs WHERE job_id = $1::uuid", job_id)
        await pg.stop()
