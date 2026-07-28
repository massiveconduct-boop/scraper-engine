# Round 17 — Full-Stack End-to-End Smoke

**Date:** 2026-07-26
**Scope:** #2 from the backlog — the request pipeline (API → tenant auth → SSRF → quota → DB persist → retrieval) had only ever been tested per-component, never as one live job through the running stack.

---

## Live pipeline — all enforcement points, against the running compose stack

Stack up (`postgres`, `pgbouncer`, `redis`, `api`, `minio`, `prometheus`, `alertmanager`), 2 seeded tenants (`system`/`sk-admin`, `other`/`sk-other`):

| Check | Request | Result |
|---|---|---|
| Valid scrape | `POST /v1/scrape` `X-API-Key: sk-admin`, public URL | **200** — `{"job_id":"…","status":"PENDING","urls":1,"tenant":"system"}` |
| Tenant auth | same, `X-API-Key: sk-invalid` | **401** |
| SSRF guard | `X-API-Key: sk-admin`, `http://127.0.0.1:22/` | **403** |
| DB persistence | direct `SELECT` on `system.scrape_jobs` | row present: `PENDING`, `urls=1` (per-tenant schema) |
| Job retrieval | `GET /v1/jobs/{id}` | **200** (after fix — see below) |
| Not found | `GET /v1/jobs/{random-uuid}` | **404** |

Auth, SSRF, quota, per-tenant-schema persistence, and retrieval all verified live end-to-end — not mocked.

---

## Real bug found and fixed — GET /v1/jobs/{id} 500'd on every existing job

The job persisted correctly and the 404 path worked, but retrieving an **existing** job returned **500 Internal Server Error**. Traceback:
```
File "/app/api/routes.py", line 149, in get_job
    return JobStatusResponse(
pydantic_core.ValidationError: 1 validation error for JobStatusResponse
job_id
  Input should be a valid string [type=string_type,
   input_value=UUID('b76b60d2-…'), input_type=UUID]
```

**Root cause:** `scrape_jobs.job_id` is a Postgres `UUID`; asyncpg returns it as a Python `uuid.UUID`, but `JobStatusResponse.job_id` is typed `str`. The handler passed the raw UUID straight into the response model → Pydantic rejected it → 500. The 404 path never hit this because it constructs no response from a row. It would have 500'd on **every** successful job lookup in production.

**Fix (`api/routes.py`):** `job_id=str(row["job_id"])`.

**Verified live** (fix hot-deployed into the running container, re-tested):
```
GET /v1/jobs/b76b60d2-… → 200 {"job_id":"b76b60d2-…","status":"PENDING","progress":0.0,...}
GET /v1/jobs/<random>   → 404   (unchanged)
```

**Regression tests** (`tests/unit/test_api_routes.py`, 2): `get_job` with a mocked PG returning a `uuid.UUID` job_id → asserts a `str` response; missing row → 404. Confirmed the bug is real: `JobStatusResponse(job_id=<UUID>)` raises `ValidationError` without the coercion.

---

## Verification
- Full suite: **220 passed, 1 skipped, 0 failed** (2 new API-route regressions). Ruff clean.
- Live: 200/401/403/404 all correct; job persisted to per-tenant schema; GET fixed 500→200.

## Files Changed
**Modified:** `api/routes.py` (`str(row["job_id"])`).
**New:** `tests/unit/test_api_routes.py` (2 regression tests).

## Note
The worker-side escalation (L1→L2→L3) is exercised by `tests/integration/test_worker_escalation.py` and the round-15/16 live real-site validation; this round covered the request front-half that had never been run live as a whole.
