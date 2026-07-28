# Scraper Engine — CLAUDE.md

Project identity, operating rules, and navigation. Updated 2026-07-27 after round 22 (execution pipeline wired end-to-end). Source code is fully implemented — not blueprint phase.

## Project Identity

Async Python multi-level web scraping system. Levels: L1 (HTTP/Scrapling), L2 (Botasaurus+Camoufox), L3 (Camoufox-only). Anti-detection, proxy management, SSRF safety, multi-tenant.

## Operating Rules

1. **Evidence over assertion.** Every claim must be backed by raw terminal output or source code reference. Never paraphrase numbers.
2. **Root cause solutions, not patches.** Remove broken code instead of deleting it; fix underlying issue. Documenting problem is not same as solving it.
3. **Invariants are non-negotiable.** 7 design invariants from `specs/scraper-engine-blueprint-v2.md` §1.1 are absolute.
4. **No transient numbers in reports.** Commit hashes and counts change on every commit — use stable references instead.
5. **Prefer `ctx_execute` over `Bash` for long-running commands.** Bash tool has 120s timeout (signal 16, exit 144). Harvest cycles take ~25s.
6. **Tests run with `docker compose up -d postgres redis pgbouncer` first.** Integration/chaos tests need infrastructure. PgBouncer must be running for G-05.

## Architecture

- **Runtime:** Python 3.12, asyncio, FastAPI, uvicorn
- **Browser:** Camoufox v0.5.4 (Firefox 152), semaphore-gated pool with `lease()` context manager
- **Proxy:** 8-URL sources across 6 operators, TCP probe + HTTP validation, two-tier scoring
- **Storage:** PostgreSQL 16 (PgBouncer transaction-pooling), Redis 7, S3/MinIO
- **Testing:** pytest 9.1.1, ~293 tests (unit+integration+chaos; 292 pass / 1 skip / 0 error as of round 22) + 12 live + load suite. Captcha/Camoufox live tests skipped in CI.
- **Execution pipeline wired end-to-end (round 22).** `POST /v1/scrape`/`POST /v1/crawl` enqueue onto real `rq` queue (`orchestrator/job_queue.py`); `orchestrator/tasks.py` is rq entry point that builds `Worker` + deps, drives `process_job`persists results (`scrape_results`S3 snapshots), and fires webhook. Live-verified: real HTTP job through running containers, `PENDING → COMPLETED`MinIO snapshot confirmed. See `.claude/MEMORY.md` → Open Threads for full evidence.
- **Security-hardened (round 22 follow-up).** SSRF re-validated at fetch time and per redirect hop, not at submit time (closed DNS-rebind TOCTOU gap — see `.claude/knowledge/architecture.md` → SSRF Enforcement). CORS no longer pairs wildcard origin with credentials. `GET /health`'s `proxy_pool_size` now reflects live pool (was always 0). Live-proven, not tested — see `.claude/MEMORY.md` → Open Threads.
- **Linting:** ruff (clean), mypy `--strict` clean (baseline retired round 18)

## Module Map

| Package | Responsibility |
|---|---|
| `core/` | Domain models, TenantId, SSRF guard, retry, budget, quota |
| `proxy/` | Harvester (multi-source + broker subprocess), Manager, Scoring, Lease, `asn_classifier.py` (real MaxMind GeoLite2-ASN classification, env-gated on `GEOIP_ASN_DB_PATH`, round 22) |
| `browser/` | CamoufoxWrapper, BrowserPool (hot-browser `lease()`), session state |
| `fetcher/` | Level1/2/3 fetchers, `factory.py` (DI, CI-gated), `_content_utils` (shared guard/poll/scroll), `challenge_detector`, `_failure` |
| `orchestrator/` | Worker (escalation state machine), CircuitBreaker, PolitenessController, `job_queue.py` (rq producer), `tasks.py` (rq consumer entry point — persists results, dispatches webhook), WebhookDispatcher |
| `api/` | FastAPI routes (wired: SSRF guard, tenant auth, per-tenant quota, DB persist, rq enqueue, composite health check). Middleware |
| `storage/` | PostgresClient (BEGIN...COMMIT PgBouncer isolation), RedisClient, S3Client, DLQ |
| `config/` | Pydantic schema, YAML loader |
| `cli/` | Entrypoint |
| `observability/` | Prometheus metrics, structured logging |
| `services/` | CAPTCHA solving — NoCaptchaAI primary + CapSolver fallback (`captcha_solver`, `nocaptcha`, `capsolver`, `_anticaptcha`). Wired into L2/L3 fetch path (round 20); key-health preflight `tools/validate_captcha_keys.py` (round 21). `scrapy_adapter.py` — bulk crawl (subprocess-isolated, round 22), `firecrawl_client.py` — markdown conversion wired into L1 (round 22, env-gated on `FIRECRAWL_API_KEY`) |

## Navigation

- **Knowledge catalog:** `.claude/MEMORY.md` — index of all knowledge documents, evidence reports, and operational references. **Read this first.**
- **Architecture:** `.claude/knowledge/architecture.md`
- **Design decisions:** `.claude/knowledge/decisions.md`
- **Standards:** `.claude/knowledge/standards.md`
- **Troubleshooting:** `.claude/knowledge/troubleshooting.md`
- **Operations:** `.claude/knowledge/operations.md`
- **Specification:** `specs/scraper-engine-blueprint-v2.md` (authoritative)
- **CI:** `.github/workflows/test.yml` (lint incl. mypy-strict + grep-gates; unit/integration/chaos) | mypy baseline retired (empty)

## Quick Commands

```bash
source .venv/bin/activate
docker compose up -d postgres redis pgbouncer && alembic upgrade head
pytest tests/unit/ tests/integration/ tests/chaos/ -q     # ~258 collected (256 pass/1 skip/1 error)
ruff check . --exclude 'challenge-mirror' --exclude 'report-review-fix'
```

