# Scraper Engine — CLAUDE.md

Project identity, operating rules, and navigation. Updated 2026-07-24 after 7 rounds of production-readiness audit. Source code is fully implemented — not blueprint phase.

## Project Identity

Async Python multi-level web scraping system. Levels: L1 (HTTP/Scrapling), L2 (Botasaurus+Camoufox), L3 (Camoufox-only). Anti-detection, proxy management, SSRF safety, multi-tenant.

## Operating Rules

1. **Evidence over assertion.** Every claim must be backed by raw terminal output or source code reference. Never paraphrase numbers.
2. **Root cause solutions, not patches.** Remove broken code instead of deleting it; fix the underlying issue. Documenting a problem is not the same as solving it.
3. **Invariants are non-negotiable.** The 7 design invariants from `specs/scraper-engine-blueprint-v2.md` §1.1 are absolute.
4. **No transient numbers in reports.** Commit hashes and counts change on every commit — use stable references instead.
5. **Prefer `ctx_execute` over `Bash` for long-running commands.** The Bash tool has a 120s timeout (signal 16, exit 144). Harvest cycles take ~25s.
6. **Tests run with `docker compose up -d postgres redis pgbouncer` first.** Integration/chaos tests need infrastructure. PgBouncer must be running for G-05.

## Architecture

- **Runtime:** Python 3.12, asyncio, FastAPI, uvicorn
- **Browser:** Camoufox v0.5.4 (Firefox 152), semaphore-gated pool with `lease()` context manager
- **Proxy:** 8-URL sources across 6 operators, TCP probe + HTTP validation, two-tier scoring
- **Storage:** PostgreSQL 16 (PgBouncer transaction-pooling), Redis 7, S3/MinIO
- **Testing:** pytest 9.1.1, 170 tests (unit + integration + chaos)
- **Linting:** ruff, mypy

## Module Map

| Package | Responsibility |
|---|---|
| `core/` | Domain models, TenantId, SSRF guard, retry, budget, quota |
| `proxy/` | Harvester (multi-source + broker subprocess), Manager, Scoring, Lease |
| `browser/` | CamoufoxWrapper, BrowserPool (hot-browser `lease()`), session state |
| `fetcher/` | Level1Fetcher (HTTP), Level2Fetcher (Camoufox), Level3Fetcher, challenge detector |
| `orchestrator/` | Worker (escalation state machine), CircuitBreaker, PolitenessController |
| `api/` | FastAPI routes, auth, health, rate limiting, CORS middleware |
| `storage/` | PostgresClient (BEGIN...COMMIT PgBouncer isolation), RedisClient, S3Client, DLQ |
| `config/` | Pydantic schema, YAML loader |
| `cli/` | Entrypoint |
| `observability/` | Prometheus metrics, structured logging |

## Navigation

- **Knowledge catalog:** `.claude/MEMORY.md` — index of all knowledge documents
- **Architecture deep dive:** `.claude/knowledge/architecture.md`
- **Design decisions & rationale:** `.claude/knowledge/decisions.md`
- **Coding standards & test patterns:** `.claude/knowledge/standards.md`
- **Troubleshooting & known bugs:** `.claude/knowledge/troubleshooting.md`
- **Operations & deployment:** `.claude/knowledge/operations.md`
- **Specification:** `specs/scraper-engine-blueprint-v2.md` (authoritative)
- **Round 6 evidence reports:** `docs/round-6-*.md`

## Quick Commands

```bash
source .venv/bin/activate
docker compose up -d postgres redis pgbouncer && alembic upgrade head
pytest tests/unit/ tests/integration/ tests/chaos/ -q     # 170 tests
ruff check . --exclude 'challenge-mirror' --exclude 'report-review-fix'
```
