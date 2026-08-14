# Scraper Engine — CLAUDE.md

Project identity, operating rules, and navigation. Currently at round 35 (proxy_exhausted root-cause fix — reverse-DNS ASN classification + self-healing daemons consolidated into one supervised container). Full round-by-round narrative, every open thread, every decision: `.claude/knowledge/technical-debt.md` (start there, not here — this file is navigation only). Source code is fully implemented — not blueprint phase.

## Project Identity

Async Python multi-level web scraping system. Levels: L1 (HTTP/Scrapling), L2 (Botasaurus+Camoufox), L3 (Camoufox-only). Anti-detection, proxy management, SSRF safety, multi-tenant.

## Operating Rules

1. **Evidence over assertion.** Every claim must be backed by raw terminal output or source code reference. Never paraphrase numbers.
2. **Root cause solutions, not patches.** Remove broken code instead of deleting it; fix underlying issue. Documenting problem is not same as solving it.
3. **Invariants are non-negotiable.** 7 design invariants from design spec (`.local/specs/scraper-engine-blueprint-v2.md`local-only, not tracked in git) §1.1 are absolute.
4. **No transient numbers in reports.** Commit hashes and counts change on every commit — use stable references instead.
5. **Prefer `ctx_execute` over `Bash` for long-running commands.** Bash tool has 120s timeout (signal 16, exit 144). Harvest cycles take ~25s.
6. **Tests run with `docker compose up -d postgres redis pgbouncer` first.** Integration/chaos tests need infrastructure. PgBouncer must be running for G-05.

## OpenWolf

@.wolf/OPENWOLF.md

Session order (quick-glance version of the import above): `.wolf/STATUS.md` first (current quest, next steps, decisions) → `.wolf/anatomy.md` before opening any file → `.wolf/cerebrum.md` Do-Not-Repeat before generating code → `.wolf/buglog.json` before fixing any bug.

### OpenWolf CLI

```bash
openwolf status      # daemon health, last session stats, file integrity
openwolf scan        # force full anatomy rescan (after adding/renaming/deleting files)
openwolf report      # token report: estimated vs measured
openwolf dashboard   # browser dashboard (127.0.0.1:18799)
openwolf bug         # bug memory management
openwolf daemon      # daemon management
openwolf cron        # cron task management
```

`openwolf scan` regenerates `.wolf/anatomy.md` from `.wolf/anatomy-index.json`. Descriptions in `anatomy.md` may be edited (they are absorbed on the next scan) but never reorder or reformat the file.

**`cerebrum.md`'s Decision Log vs `.claude/knowledge/decisions.md` (round 34):** these overlap in purpose and already drifted once (see `decisions.md` → "OpenWolf ↔ `.claude/knowledge/` Division of Labor"). `cerebrum.md` stays OpenWolf's fast session-local capture — don't hand-edit it. Any decision logged there with real lasting architectural consequence must also be written to `.claude/knowledge/decisions.md` in the same session — that file is what `CLAUDE.md`'s Navigation section actually sends readers to.

## Architecture

- **Runtime:** Python 3.12, asyncio, FastAPI, uvicorn
- **Browser:** Camoufox v0.5.4 (Firefox 152), semaphore-gated pool with `lease()` context manager
- **Proxy:** 8-URL sources across 6 operators, TCP probe + HTTP validation, two-tier scoring
- **Storage:** PostgreSQL 16 (PgBouncer transaction-pooling), Redis 7, S3/MinIO
- **Testing:** pytest 9.1.1, unit+integration+chaos suite passes at a real 100% coverage on a correctly-set-up host (see CI, not hardcoded count here per operating rule #4) + 18 live + load suite. Captcha/Camoufox live tests skipped in CI (no Camoufox binary there). Coverage gate (`fail_under=100` in `pyproject.toml`) is wired for real (round 28) — CI's `chaos` job runs full suite with `--cov-fail-under=100`. Round 35 closed what were previously two documented local-environment exclusions, both root-caused as sandbox/venv corruption rather than real gaps: (1) `services/botasaurus_requests_client.py` "aarch64 sandbox" failures were `unittest.mock.patch("botasaurus_requests.session.firefox")` force-importing the real package, which loads a platform-specific native `.so` via ctypes at import time (fixed test-side by stubbing the module in `sys.modules` before import, see `tests/unit/test_botasaurus_requests_client.py`); (2) `browser/` chaos races were failing because this host's `playwright`/Camoufox binaries had been installed for the wrong CPU architecture (x86-64 artifacts on an aarch64 box) — fixed by reinstalling the correct-arch `playwright` wheel and re-running `camoufox fetch` after clearing the stale cached binary; the chaos tests also need `tests/fixtures/challenge_mirror`'s server running locally (`python -m app.server`, port 8090) which isn't started automatically. A round-34 knowledge audit caught and same-day-closed a regression to 97.91%; see `.claude/knowledge/technical-debt.md` round-34 entry, "Coverage gap" note, for detail.
- **Linting:** ruff (clean), mypy `--strict` clean (baseline retired round 18)
- **Evolution history (round-by-round narrative moved out of this file in
   round-28 knowledge-architecture audit — was 7 growing paragraphs
  here, all duplicated in more permanent form below):** execution pipeline
  wiring + SSRF hardening (round 22), observability/tracing (round 24),
  BrowserPool/CapSolver/Botasaurus wiring (round 25), Botasaurus capability
  upgrade (round 26), src/ layout consolidation (round 27), coverage gate
  + governance + dead-code wiring (round 28), caller-experience gap
  closure + caching (round 29), proxy self-healing + notification-system
  rewrite + transient DLQ auto-retry (round 34 — rounds 30-33 not
  backfilled here, see technical-debt.md's header note), proxy_exhausted
  root-cause fix — reverse-DNS ASN classification + self-healing daemons
  consolidated into one supervised container (round 35). Current design, topic-
  organized: `.claude/knowledge/architecture.md`. Full chronological
  history, every bug found, every decision: `.claude/knowledge/
  technical-debt.md`. WHY each call was made: `.claude/knowledge/
  decisions.md`.

## Module Map

All packages below live under `src/scraper_engine/` (e.g. `core/` means
`src/scraper_engine/core/`imported as `scraper_engine.core`) — moved there
from repo-root-level packages in src-layout consolidation.

| Package | Responsibility |
|---|---|
| `core/` | Domain models, TenantId, SSRF guard, retry, budget, quota, `periodic.py` (round 34 — shared `run_periodic` loop helper, extracted from `proxy/harvester_daemon.py` so `orchestrator/webhook_sweeper.py` and `proxy/dlq_reaper.py` reuse it instead of re-implementing) |
| `proxy/` | Harvester (multi-source + broker subprocess), Manager (round 34 — exhaustion now sets a debounced Redis kick key + publishes, waking `harvester_daemon.py`'s fast-poll watcher instead of waiting on the timer), Scoring, Lease, `asn_classifier.py` (round 35 — real ASN classification via reverse-DNS PTR hostname lookup, no external account/db needed, replaced the never-actually-wired MaxMind path; unconditional, no env gate), `pool_health.py` (round 34 — per-tier HEALTHY/DEGRADED/CRITICAL state machine, drives both the ops-Slack path and DLQ auto-retry eligibility), `dlq_reaper.py` (round 34 — standalone daemon, auto-retries transient DLQ entries once their condition clears; round 35 — runs as a supervised process inside the `api` container, not its own compose service) |
| `browser/` | CamoufoxWrapper (now takes `geoip`/`humanize`/`headless_mode`, round 25), session state. `pool.py::BrowserPool` (hot-browser `lease()`) is wired into L2/L3 as of round 25 — one pool per rq job (see `.claude/knowledge/architecture.md` → "Browser Pool"). `botasaurus_pool.py::BotasaurusPool` (round 26) — same one-per-rq-job shape, reuses one live Botasaurus driver across same-domain URLs |
| `fetcher/` | Level1/2/3 fetchers, `factory.py` (DI, CI-gated), `_content_utils` (shared guard/poll/scroll), `challenge_detector`, `_failure`, `_captcha.py` (DOM detect→solve→inject→re-poll, round 20), `botasaurus_wrapper.py` (round 25 — real Botasaurus fetch attempt tried before the Camoufox pipeline in L2; capability-upgraded round 26, see `.claude/knowledge/architecture.md` → "Botasaurus Integration" / "Botasaurus Capability Upgrade"), `scrapling_wrapper.py` (round 28 — L1's third first-attempt engine, gated on `config.levels.level_1.engine == "scrapling"`), `adaptive_selector.py` (round 28 — structured extraction, called from `Worker.process_job`, not from within `fetcher/`). `level_1.py` no longer touches Firecrawl at all (round 29 — moved to `orchestrator/worker.py`, see below) |
| `orchestrator/` | Worker (escalation state machine — round 29 adds a `CACHE_TTL_DAYS`-gated cache-reuse check per URL, a cooperative `_is_cancelled` mid-loop check, and centralizes markdown conversion here instead of L1; round 34 adds `PERMANENT_FAILURE_CATEGORIES`/`TRANSIENT_FAILURE_CATEGORIES` DLQ split and `partial_failure` derivation), CircuitBreaker, PolitenessController, `job_queue.py` (rq producer), `tasks.py` (rq consumer entry point — round 29: `_persist_one_result` persists each result as it lands via `Worker.process_job`'s `on_result` callback instead of batching at the end; round 34: webhook dispatch now goes through the durable outbox, `_persist_one_result` also clears stale DLQ entries on success), `WebhookDispatcher` (round 34 — config-driven retry/timeout, accepts pre-rendered payloads not just `JobStatusResponse`), `webhook_events.py`/`webhook_dispatch.py`/`webhook_sweeper.py`/`slack_formatter.py` (round 34 — event taxonomy, durable outbox writer, standalone retry-sweeper daemon, Slack Block Kit rendering; see `.claude/knowledge/architecture.md` → "Notifications & Proxy Self-Healing") |
| `api/` | FastAPI routes (wired: SSRF guard, tenant auth, per-tenant quota, DB persist, rq enqueue, composite health check). Round 29 adds `Idempotency-Key` dedup on `/v1/scrape`+`/v1/crawl` (before the quota charge), `GET /v1/jobs/{job_id}/dlq`, and `DELETE /v1/jobs/{job_id}` (cancellation). Round 34: the `webhook` field is now SSRF-guarded too, same checkpoint as scrape target URLs. Middleware |
| `storage/` | PostgresClient (BEGIN...COMMIT PgBouncer isolation), RedisClient, S3Client (round 29 — `SUCCESS_RETENTION_DAYS` bumped 1→7 to match the new cache window), DLQ (round 29 — `list_for_tenant` takes an optional `job_id` filter; round 34 — UPSERTs on `(job_id, url)`, `retry()` removed in favor of `mark_retry_attempt`/`clear`, gained `auto_retry_count`), `webhook_outbox.py` (round 34 — transactional outbox mirroring DLQ's shape) |
| `config/` | Pydantic schema, YAML loader |
| `cli/` | Entrypoint |
| `observability/` | `bootstrap.py` (round 24 — single call wiring logging+tracing into every process), structured JSON logging (`logging.py`, stdlib-bridged via `ProcessorFormatter`), real distributed tracing (`tracing.py` — Jaeger + httpx/asyncpg/redis auto-instrumentation), Prometheus metrics |
| `services/` | CAPTCHA solving — NoCaptchaAI primary + CapSolver fallback (`captcha_solver`, `nocaptcha`, `capsolver`, `_anticaptcha`). Wired into L2/L3 fetch path (round 20); key-health preflight `tools/validate_captcha_keys.py` (round 21). `scrapy_adapter.py` — bulk crawl (subprocess-isolated, round 22), `firecrawl_client.py` — markdown conversion, wired into `orchestrator/worker.py` as of round 29 (was L1-only since round 22) so it applies regardless of escalation level; env-gated on `FIRECRAWL_API_KEY` (hosted) or `FIRECRAWL_BASE_URL` (self-hosted, round 29 — no key required), `markdown_fallback.py` (round 29 — local HTML→Markdown when Firecrawl isn't configured), `botasaurus_requests_client.py` — JA3-TLS-fingerprint client wired into L1 (round 26, config-gated on `config.botasaurus.l1_ja3_client_enabled`, default off), `extraction_engine_client.py` — optional schema-driven extraction, env-gated on `EXTRACTION_ENGINE_BASE_URL`, falls back to `AdaptiveSelector` when unset or on failure |
| `scrapy_project/` | Scrapy spider project backing `services/scrapy_adapter.py`'s subprocess-isolated bulk-crawl path (`spiders/`, `pipelines/`, `middlewares/`, `addons.py`, `settings.py`) |

## Navigation

- **Knowledge catalog:** `.claude/MEMORY.md` — index of all knowledge documents, evidence reports, and operational references. **Read this first.**
- **Architecture:** `.claude/knowledge/architecture.md`
- **Design decisions:** `.claude/knowledge/decisions.md`
- **Standards:** `.claude/knowledge/standards.md`
- **Troubleshooting:** `.claude/knowledge/troubleshooting.md`
- **Operations:** `.claude/knowledge/operations.md`
- **Technical debt & full round history:** `.claude/knowledge/technical-debt.md` (not force-loaded — open it when you need full story, not every session)
- **Specification:** `.local/specs/scraper-engine-blueprint-v2.md` (authoritative, local-only — not tracked in git)
- **CI:** `.github/workflows/test.yml` (lint incl. mypy-strict + grep-gates; unit/integration/chaos with real PgBouncer via docker compose, round 23; build-and-push to GHCR on merge to main, round 22) | mypy baseline retired (empty)

## Quick Commands

```bash
source .venv/bin/activate
pre-commit install  # one-time per clone
docker compose up -d postgres redis pgbouncer minio migrate   # migrate applies alembic upgrade head, then exits
pytest tests/unit/ tests/integration/ tests/chaos/ --cov=src/scraper_engine --cov-fail-under=100   # pass count: see CI, not hardcoded here per operating rule #4
ruff check . --exclude 'tests/fixtures/challenge_mirror'
```

