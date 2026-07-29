# Scraper Engine — CLAUDE.md

Project identity, operating rules, and navigation. Updated 2026-07-29 after round 28 (real coverage gate wired to 100% + 7 other senior-dev-review findings closed — version, CI Python matrix, dependency lockfile, vulnerability scanning, `py.typed`, governance files, pre-commit hooks; see `.claude/MEMORY.md` → Technical Debt for full round-28 story). Source code is fully implemented — not blueprint phase.

## Project Identity

Async Python multi-level web scraping system. Levels: L1 (HTTP/Scrapling), L2 (Botasaurus+Camoufox), L3 (Camoufox-only). Anti-detection, proxy management, SSRF safety, multi-tenant.

## Operating Rules

1. **Evidence over assertion.** Every claim must be backed by raw terminal output or source code reference. Never paraphrase numbers.
2. **Root cause solutions, not patches.** Remove broken code instead of deleting it; fix underlying issue. Documenting problem is not same as solving it.
3. **Invariants are non-negotiable.** 7 design invariants from design spec (`.local/specs/scraper-engine-blueprint-v2.md`local-only, not tracked in git) §1.1 are absolute.
4. **No transient numbers in reports.** Commit hashes and counts change on every commit — use stable references instead.
5. **Prefer `ctx_execute` over `Bash` for long-running commands.** Bash tool has 120s timeout (signal 16, exit 144). Harvest cycles take ~25s.
6. **Tests run with `docker compose up -d postgres redis pgbouncer` first.** Integration/chaos tests need infrastructure. PgBouncer must be running for G-05.

## Architecture

- **Runtime:** Python 3.12, asyncio, FastAPI, uvicorn
- **Browser:** Camoufox v0.5.4 (Firefox 152), semaphore-gated pool with `lease()` context manager
- **Proxy:** 8-URL sources across 6 operators, TCP probe + HTTP validation, two-tier scoring
- **Storage:** PostgreSQL 16 (PgBouncer transaction-pooling), Redis 7, S3/MinIO
- **Testing:** pytest 9.1.1, 611 unit+integration+chaos tests pass (1 skip, 0 error, as of round 28) + 12 live + load suite. Captcha/Camoufox live tests skipped in CI. **Coverage gate (`fail_under=100` in `pyproject.toml`) is wired for real (round 28) — CI's `chaos` job runs the full suite with `--cov-fail-under=100`. Real measured coverage: 100% across every package in `[tool.coverage.run] source` except `browser/` (needs real Firefox, not available in CI).**
- **Execution pipeline wired end-to-end (round 22).** `POST /v1/scrape`/`POST /v1/crawl` enqueue onto real `rq` queue (`orchestrator/job_queue.py`); `orchestrator/tasks.py` is rq entry point that builds `Worker` + deps, drives `process_job`persists results (`scrape_results`S3 snapshots), and fires webhook. Live-verified: real HTTP job through running containers, `PENDING → COMPLETED`MinIO snapshot confirmed. See `.claude/MEMORY.md` → Open Threads for full evidence.
- **Security-hardened (round 22 follow-up).** SSRF re-validated at fetch time and per redirect hop, not at submit time (closed DNS-rebind TOCTOU gap — see `.claude/knowledge/architecture.md` → SSRF Enforcement). CORS no longer pairs wildcard origin with credentials. `GET /health`'s `proxy_pool_size` now reflects live pool (was always 0). Live-proven, not tested — see `.claude/MEMORY.md` → Open Threads.
- **Observability fully wired + real tracing deployed (round 24, PR #7).** Structured JSON logging and tracing were previously never invoked (`configure_logging()`/`configure_tracing()` had zero call sites) — now bootstrapped once per process via `observability/bootstrap.py`. Real distributed tracing: Jaeger (`docker-compose.yml`UI `:16686`) + process-wide httpx/asyncpg/redis auto-instrumentation + `scrape_job` root span per rq job + `proxy_daemon_{name}` span per harvester cycle. Live-verified via Jaeger's own query API (not log silence). `/metrics` now respects `metrics_enabled` `SSRFGuard` accepts `additional_denied_cidrs`new retention reaper enforces `browser_sessions`/`domain_ban_history` TTLs. See `.claude/knowledge/architecture.md` → "Observability & Tracing" `.claude/MEMORY.md` → Open Threads (round 24) for full story, including subtle rq-fork/`BatchSpanProcessor` bug found and fixed along way.
- **Round 24's 5 unwired-config gaps closed (round 25), plus 3 more found same way + Botasaurus restored for real.** `BrowserPool` is now constructed per-job and leased by L2/L3 (rq forks fresh process per job, so pool's lifetime is one job, not one process); `CapSolverBudget` now reads real per-tenant ceiling (`tenants.capsolver_daily_credit_ceiling`), and real bug was found alongside it — `_spend_key()` ignored `tenant_id` entirely, pooling every tenant's spend into one global counter; Camoufox's `geoip`/`humanize`/`headless_mode`/`max_total_instances` now flow from config; `fetcher/botasaurus_wrapper.py` was deleted as dead code, then **restored and wired for real** per authoritative spec §3.6 after follow-up ask — `Level2Fetcher` now tries Botasaurus first, falling back to existing Camoufox pipeline on failure or detected challenge page; `PgBouncerConfig` documented informational-only (unchanged, cosmetic). Also found + fixed: 7 dead Prometheus alert metrics (rq's fork-per-job model means in-process gauges from worker code never reach `/metrics` — fixed via Redis/Postgres-backed scrape-time refresh, see `.claude/knowledge/architecture.md` → "Metrics: Cross-Process Emission Pattern"), `BrowserPool` correctness bug where ANY mismatch (not idle timeout) destroyed live browser instead of keeping it pooled, `proxy_source_healthy` having same cross-process gap as alert metrics. Full detail: `.claude/MEMORY.md` → Technical Debt (round 25). Botasaurus capability-upgrade research (not yet implemented — next quest) also in that section.
- **Botasaurus capability upgrade (round 26).** Every API re-verified against real installed `botasaurus`/`botasaurus_driver`/`botasaurus_requests` source before wiring, not README. `google_get(bypass_cloudflare=True)` replaces plain `get()` in `fetcher/botasaurus_wrapper.py` (free Cloudflare-tier bypass), plus `tiny_profile`/`remove_default_browser_check_argument`/`close_on_crash`/`max_retry`/`HASHED` fingerprinting, all config-driven via new `config.botasaurus: BotasaurusConfig`. New `browser/botasaurus_pool.py::BotasaurusPool` reuses one live driver across same-domain URLs within job (opt-in, one per rq job like `BrowserPool`) — deliberately does **not** use botasaurus's own `reuse_driver=True`since reading its source found that mechanism is unkeyed global pool that would leak proxy/tenant state across fetches (spec §1.1 #3 risk). New `services/botasaurus_requests_client.py` wires JA3-TLS-fingerprint-matched client into L1, config-gated off by default. Live end-to-end verification against `challenge-mirror` initially failed and was first misdiagnosed as Chrome/CDP version mismatch — real cause was genuine bug in `fetcher/botasaurus_wrapper.py`botasaurus's decorator always calls wrapped function positionally (`func(driver, data)`), which silently clobbered keyword-default `target_url` param with `None`so every fetch navigated nowhere. Fixed (read URL from closure instead), then live-confirmed for real: `google_get(bypass_cloudflare=True)` fetches real content from `challenge-mirror` `BotasaurusPool` reuse is confirmed via exactly one `Driver()` construction across two same-domain fetches. Full story: `.claude/knowledge/architecture.md` → "Botasaurus Capability Upgrade".
- **Repo layout professionalization + src/ consolidation (round 27).** Historical per-round reports moved out of `docs/` into gitignored `.archive/{evidence,directive,closure,other}/` (categorized, kept on disk, off GitHub); `challenge-mirror/`  `judge_server.py` (real test infra) moved to `tests/fixtures/` `specs/` and unused scripts moved to separate gitignored `.local/`. Added `LICENSE` (Apache 2.0), `NOTICE` `CONTRIBUTING.md` `CHANGELOG.md`. `alembic.ini`'s `script_location` fixed to be cwd-independent (`%(here)s` token) — documented production migration command was silently broken inside containers. **Then 12 top-level packages were consolidated under `src/scraper_engine/`** (455 import statements rewritten) — see Module Map below `.claude/knowledge/architecture.md` → "Repository Layout" for full story, including three real bugs rewrite surfaced (a parameter/module name-shadowing near-miss, string-based `mock.patch`/rq-job-queue module references invisible to static import checks, `types-redis` stub-vs-real-types CI/local mismatch). Verified beyond static analysis: real job submitted through rebuilt live API went `PENDING → COMPLETED`.
- **Coverage gate wired for real + 7 other senior-dev-review findings closed (round 28).** `pyproject.toml`'s `fail_under` was declared (90, then 100) but no CI `pytest` invocation ever passed `--cov` — the gate never ran; real measured coverage was 72%. Now wired into CI's `chaos` job and brought to 100% (~370 missing lines closed across ~20 files, `fetcher/`+`services/` were the bulk of the gap). Two real bugs found writing the tests: `fetcher/scrapling_wrapper.py` called a nonexistent `scrapling.get()` (fixed to the real `scrapling.fetchers.AsyncFetcher.get()` API), and `pyproject.toml`'s `dependencies = [...]` was TOML-nested under the wrong table, so `pip install -e .` had been installing zero runtime dependencies. Also: version `0.1.0`→`1.0.0` + `CHANGELOG.md` + a documented release process; CI Python matrix now 3.11+3.12; `requirements-lock.txt`/`requirements-dev-lock.txt` (`uv pip compile`) replace 3 hand-duplicated dependency lists in CI/Dockerfile, with a CI drift check; `pip-audit` + Dependabot; `py.typed`; `SECURITY.md`/`CODEOWNERS`/issue-PR templates; `.pre-commit-config.yaml` (ruff + scoped `mypy --strict`, verified via `pre-commit run --all-files`). Full story: `.claude/MEMORY.md` → Technical Debt (round 28).
- **Linting:** ruff (clean), mypy `--strict` clean (baseline retired round 18)

## Module Map

All packages below live under `src/scraper_engine/` (e.g. `core/` means
`src/scraper_engine/core/`imported as `scraper_engine.core`) — moved there
from repo-root-level packages in src-layout consolidation.

| Package | Responsibility |
|---|---|
| `core/` | Domain models, TenantId, SSRF guard, retry, budget, quota |
| `proxy/` | Harvester (multi-source + broker subprocess), Manager, Scoring, Lease, `asn_classifier.py` (real MaxMind GeoLite2-ASN classification, env-gated on `GEOIP_ASN_DB_PATH`, round 22) |
| `browser/` | CamoufoxWrapper (now takes `geoip`/`humanize`/`headless_mode`, round 25), session state. `pool.py::BrowserPool` (hot-browser `lease()`) is wired into L2/L3 as of round 25 — one pool per rq job (see `.claude/knowledge/architecture.md` → "Browser Pool"). `botasaurus_pool.py::BotasaurusPool` (round 26) — same one-per-rq-job shape, reuses one live Botasaurus driver across same-domain URLs |
| `fetcher/` | Level1/2/3 fetchers, `factory.py` (DI, CI-gated), `_content_utils` (shared guard/poll/scroll), `challenge_detector`, `_failure`, `botasaurus_wrapper.py` (round 25 — real Botasaurus fetch attempt tried before the Camoufox pipeline in L2; capability-upgraded round 26, see `.claude/knowledge/architecture.md` → "Botasaurus Integration" / "Botasaurus Capability Upgrade") |
| `orchestrator/` | Worker (escalation state machine), CircuitBreaker, PolitenessController, `job_queue.py` (rq producer), `tasks.py` (rq consumer entry point — persists results, dispatches webhook), WebhookDispatcher |
| `api/` | FastAPI routes (wired: SSRF guard, tenant auth, per-tenant quota, DB persist, rq enqueue, composite health check). Middleware |
| `storage/` | PostgresClient (BEGIN...COMMIT PgBouncer isolation), RedisClient, S3Client, DLQ |
| `config/` | Pydantic schema, YAML loader |
| `cli/` | Entrypoint |
| `observability/` | `bootstrap.py` (round 24 — single call wiring logging+tracing into every process), structured JSON logging (`logging.py`, stdlib-bridged via `ProcessorFormatter`), real distributed tracing (`tracing.py` — Jaeger + httpx/asyncpg/redis auto-instrumentation), Prometheus metrics |
| `services/` | CAPTCHA solving — NoCaptchaAI primary + CapSolver fallback (`captcha_solver`, `nocaptcha`, `capsolver`, `_anticaptcha`). Wired into L2/L3 fetch path (round 20); key-health preflight `tools/validate_captcha_keys.py` (round 21). `scrapy_adapter.py` — bulk crawl (subprocess-isolated, round 22), `firecrawl_client.py` — markdown conversion wired into L1 (round 22, env-gated on `FIRECRAWL_API_KEY`), `botasaurus_requests_client.py` — JA3-TLS-fingerprint client wired into L1 (round 26, config-gated on `config.botasaurus.l1_ja3_client_enabled`, default off) |

## Navigation

- **Knowledge catalog:** `.claude/MEMORY.md` — index of all knowledge documents, evidence reports, and operational references. **Read this first.**
- **Architecture:** `.claude/knowledge/architecture.md`
- **Design decisions:** `.claude/knowledge/decisions.md`
- **Standards:** `.claude/knowledge/standards.md`
- **Troubleshooting:** `.claude/knowledge/troubleshooting.md`
- **Operations:** `.claude/knowledge/operations.md`
- **Specification:** `.local/specs/scraper-engine-blueprint-v2.md` (authoritative, local-only — not tracked in git)
- **CI:** `.github/workflows/test.yml` (lint incl. mypy-strict + grep-gates; unit/integration/chaos with real PgBouncer via docker compose, round 23; build-and-push to GHCR on merge to main, round 22) | mypy baseline retired (empty)

## Quick Commands

```bash
source .venv/bin/activate
pre-commit install  # one-time per clone
docker compose up -d postgres redis pgbouncer minio && alembic upgrade head
pytest tests/unit/ tests/integration/ tests/chaos/ --cov=src/scraper_engine --cov-fail-under=100   # 611 pass / 1 skip / 0 error / 100% (round 28)
ruff check . --exclude 'tests/fixtures/challenge_mirror'
```

