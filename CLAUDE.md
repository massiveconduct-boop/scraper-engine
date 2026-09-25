# Scraper Engine — CLAUDE.md

Project identity, operating rules, and navigation. Currently at round 69.
**Never a diary.** Audits at rounds 28, 57 and 66 each removed dated
per-round narrative from this exact spot (`decisions.md` →
"Knowledge-Audit: Round-57 CLAUDE.md Diary Regression"); a 1800-word gate
(`tools/check_claude_md_size.sh`) now fails the build on regrowth. A new
round's story goes in `.claude/knowledge/technical-debt.md` — start there,
not here — and, if it fits in a clause, in "Evolution history" below.
Source code is fully implemented, not blueprint phase.

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

**`cerebrum.md`'s Decision Log vs `.claude/knowledge/decisions.md` (round 34):** these overlap in purpose and already drifted once (see `decisions.md` → "OpenWolf ↔ `.claude/knowledge/` Division of Labor"). `cerebrum.md` stays OpenWolf's fast session-local capture, updated per the protocol above. Any decision logged there with real lasting architectural consequence must also be written to `.claude/knowledge/decisions.md` in the same session — that file is what `CLAUDE.md`'s Navigation section actually sends readers to.

## Architecture

- **Runtime:** Python 3.12, asyncio, FastAPI, uvicorn
- **Browser:** Camoufox v0.5.4 (Firefox 152), semaphore-gated pool with `lease()` context manager
- **Proxy:** 10 source URLs across 8 operators, TCP probe + HTTP validation, two-tier scoring
- **Storage:** PostgreSQL 16 (PgBouncer transaction-pooling), Redis 7, S3/MinIO
- **Testing:** pytest 9.1.1, unit+integration+chaos suite + live + load suites (counts: see CI, not hardcoded here per operating rule #4). Captcha/Camoufox live tests skipped in CI (no Camoufox binary there). **Coverage gate: the real gate is `tools/check_coverage_ratchet.py` — zero missed LINES plus an absolute missed-BRANCH budget that may only shrink, at 0 since round 64; `--cov-fail-under=100` is a backstop.** It covers 10 packages (core, proxy, orchestrator, fetcher, services, storage, api, scrapy_project, cli, observability); `browser/` is measured-but-ungated (needs a real Firefox) and `config/` is outside it. Chaos tests also need `tests/fixtures/challenge_mirror`'s server running locally (`python -m app.server`, port 8090), which isn't started automatically. History of the gate's exclusions, regressions and audits (rounds 34, 35, 62, 64): `.claude/knowledge/technical-debt.md`.
- **Linting:** ruff (clean), mypy `--strict` clean (baseline retired round 18)
- **Evolution history (one clause per round; a round-28 audit moved the
  full narrative out of this file, round-57 and round-66 audits re-trimmed
  regrowths — see decisions.md → "Knowledge-Audit: Round-57 CLAUDE.md Diary
  Regression". Rounds 22-62 are one line each here; the story is in
  technical-debt.md):** pipeline + SSRF + observability + pools + src/
  layout + coverage gate + caching (22-29), proxy self-healing, ASN
  classification, daemon liveness and L2/L3 leasing (34-37), harvest growth
  and scoring fixes (38-39), paid gateway + Xvfb contention (40-41),
  failure-category, circuit-breaker and challenge-detection fixes (42-46),
  env-overridable toggles + gateway-fallback gaps + concurrent dispatch
  (47-49), fingerprint crash + DLQ/orphan-job reliability (50-55),
  unrouted capabilities as API/CLI (56), Botasaurus false-success,
  autoscroll, `block_images`, extensions/locale/mouse/network capture
  (57-60), CAPTCHA no-plan check + politeness slot retry (61),
  gateway exit-IP rotation + measured ASN pin + branch-coverage ratchet
  (62), bulk-crawl throughput — per-domain level memory, politeness
  rewrite, stuck-job reaper, browser-permit deadlock, per-phase timing
  (63), a remaining-issues sweep — gateway retry at every level,
  `escalations`, cross-engine browser permits, crawl SSRF on redirects,
  per-domain skip hints, 0-branch gate over 10 packages (64), host-wide
  browser admission — seat + politeness slot claimed together,
  pressure-sized, off by default (65), a proxy's 407 as its own
  `proxy_auth_failed` category, a controller that only limits a strained
  host, `display_lock_wait_ms` (66), parked browsers keeping their host
  seat, measured browser weight (67), a refused gateway out of use for
  every worker, `free_first` falling back to the pool (68),
  self-describing failures (69).
  Current design, topic-organized: `.claude/knowledge/architecture.md`.
  Full chronological history, every bug, every root cause:
  `.claude/knowledge/technical-debt.md`. WHY each call was made:
  `.claude/knowledge/decisions.md`.

## Module Map

All packages below live under `src/scraper_engine/` (e.g. `core/` means
`src/scraper_engine/core/`imported as `scraper_engine.core`) — moved there
from repo-root-level packages in src-layout consolidation. This table is
current-state only, no round citations — for how any of this got here,
see `.claude/knowledge/architecture.md` (design) and
`.claude/knowledge/technical-debt.md` (full round-by-round history).

| Package | Responsibility |
|---|---|
| `core/` | Domain models, TenantId, SSRF guard, retry, budget, quota. `periodic.py` — shared polling-loop helper (with optional Redis liveness heartbeat) reused by the webhook sweeper and DLQ reaper. `budget.py` — `XVFB_LOCK` (process-wide lock serializing headful-browser display spinup/teardown), `resolve_browser_max_total_instances()` (opt-in RAM-aware ceiling on live browser instances) and `acquire_browser_permit()` — the one way ANY engine takes a `BROWSER_SEMAPHORE` permit: reclaims parked instances via registered pools and counts waiters, so a pool hands a returning instance's permit over instead of parking it. `models.py` — `Proxy` (paid-gateway auth fields), `FetchResult` (`network_events` from Botasaurus CDP capture). `startup.py` — `wait_for_dependency()`, the unbounded-by-default dependency wait every process's startup uses instead of exiting when Postgres/Redis/S3 isn't up yet. `host_identity.py` — which physical host a process is on (`SCRAPER_HOST_ID`, else kernel `boot_id`). `browser_rss.py` — what this process's live browsers weigh |
| `proxy/` | Harvester (multi-source + broker subprocess), Manager (leasing, TCP+HTTPS-CONNECT preflight, SQL-side candidate exclusion, exhaustion wakes the harvester), `net_probe.py` (shared probe primitives), Scoring, Lease, `asn_classifier.py` (reverse-DNS ASN classification), `health_monitor.py` (rolling re-validation + rescoring), `pool_health.py` (per-tier HEALTHY/DEGRADED/CRITICAL state machine), `dlq_reaper.py` (transient-DLQ auto-retry daemon), `gateway_health.py` (shared "gateway is refusing our credentials" verdict), `paid_gateway.py` (toggleable DataImpulse gateway proxy; renders the provider's username grammar — per-attempt `sessid` rotation for a fresh exit IP, optional `asn` pin, `cr` country), `retention_reaper.py` |
| `browser/` | `CamoufoxWrapper` (geoip/humanize/headless, geoip-launch fallback, holds `XVFB_LOCK`), `pool.py::BrowserPool` (hot-browser `lease()`, one per rq job), `botasaurus_pool.py::BotasaurusPool` (up to `max_pooled_drivers` drivers per job, reused per proxy-identity+domain, navigated for real, display lock around launch/close only; `block_images`, `extensions`, `lang`/locale/timezone spoof, human-mode mouse, network-event capture — all opt-in via `BotasaurusConfig`), `_xvfb_cleanup.py`, `_botasaurus_nav_check.py` (`chrome-error://` silent-failure detection), `_botasaurus_scroll.py` (autoscroll port for Botasaurus's sync Driver API), `_botasaurus_extension.py`, `_botasaurus_network_capture.py` |
| `fetcher/` | Level1/2/3 fetchers, `factory.py` (DI, CI-gated), `_content_utils` (shared guard/poll/scroll), `challenge_detector` (incl. Chromium net-error structural check), `_failure`, `_captcha.py` (DOM detect→solve→inject→re-poll), `botasaurus_wrapper.py` (Botasaurus first-attempt, same feature set as `botasaurus_pool`), `level_2.py` (Botasaurus→Camoufox fallback; the real `FetchResult`-construction site for L2, including `network_events`), `scrapling_wrapper.py` (L1's third engine option), `adaptive_selector.py` (structured extraction, called from `Worker`) |
| `orchestrator/` | `Worker` — escalation state machine: cache-reuse check, cooperative cancellation, DLQ transient/permanent split, two independent retry budgets (one same-level free-pool retry on proxy-attributable failure; a separate `rotate_on_block_retries` gateway budget that rotates the exit IP on a detected block), concurrent URL dispatch bounded by `politeness.max_concurrent_urls_per_job` (default 5, `asyncio.Semaphore`), per-URL `timings` breakdown, and a per-domain start level from `level_memory`. `CircuitBreaker`, `PolitenessController`, `job_queue.py` (rq producer), `tasks.py` (rq consumer — persists each result as it lands, webhook outbox, `network_events` column), `level_memory.py` (per-domain `DomainPlan`: where to start the ladder, whether to skip the free proxy pool, whether to skip Botasaurus at L2 — skip-only, with a periodic re-probe), `host_capacity.py` (host-wide browser admission: one Lua claim grants a browser seat, the website's politeness slot and its delay together per render; leases with their own expiry; off unless `HOST_CAPACITY_ENABLED`), `capacity_controller.py` (supervisord daemon sizing that budget from host CPU PSI / MemAvailable), `stuck_job_reaper.py` (reconciles PENDING/PROCESSING rows against rq — reachability is checked against rq's OWN registry keys and `rq:executions:{job_id}`, never hardcoded key names or bare-id `zscore`, which is what made it reap live jobs; `updated_at` is also touched per result so "stale" means "not progressing"), `WebhookDispatcher`, `webhook_events.py`/`webhook_dispatch.py`/`webhook_sweeper.py`/`slack_formatter.py` |
| `api/` | FastAPI routes — SSRF guard (scrape targets and webhook URLs), tenant auth, per-tenant quota, DB persist, rq enqueue (fails loud on enqueue error, no orphaned PENDING rows), composite health check with per-daemon liveness, `Idempotency-Key` dedup, job cancellation, `GET /v1/jobs`/`/v1/quota`/`/v1/dlq`/`/v1/webhook-events`, Middleware |
| `storage/` | `PostgresClient` (BEGIN...COMMIT PgBouncer isolation), `RedisClient`, `S3Client`, `DLQ` (UPSERT on `(job_id, url)`, `auto_retry_count`), `webhook_outbox.py` (transactional outbox) |
| `config/` | Pydantic schema, YAML loader |
| `cli/` | Entrypoint. Ops subcommands (`serve`/`worker`/`harvest`/`reap`/`check`/`create-tenant`) talk directly to Postgres/Redis. Caller-facing `api` subcommand group (`scrape`/`jobs`/`job`/`quota`/`dlq`) wraps the real `/v1` HTTP API via `httpx` instead of touching storage directly. Inside the coverage gate since round 64 |
| `observability/` | `bootstrap.py` (wires logging+tracing per process), structured JSON logging, distributed tracing (Jaeger + httpx/asyncpg/redis auto-instrumentation), Prometheus metrics |
| `services/` | CAPTCHA solving — NoCaptchaAI primary + CapSolver fallback, key-health preflight tool. `scrapy_adapter.py` (subprocess-isolated bulk crawl), `firecrawl_client.py` + `markdown_fallback.py` (markdown conversion, applies at any escalation level), `botasaurus_requests_client.py` (JA3-TLS L1 client, opt-in), `extraction_engine_client.py` (optional schema-driven extraction, falls back to `AdaptiveSelector`) |
| `scrapy_project/` | Settings + downloader middlewares/pipeline for `services/scrapy_adapter.py`'s subprocess-isolated bulk crawl (`POST /v1/crawl`): `SSRFMiddleware` (every request incl. each redirect hop), `ProxyMiddleware` (the proxy `orchestrator/tasks.py::_run_crawl_job` leased, passed in as `CRAWL_PROXY_URL`), `DedupPipeline`. The spider itself is defined inline in the adapter; items are collected after the pipelines via `item_scraped` |

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
pytest tests/unit/ tests/integration/ tests/chaos/ --cov=src/scraper_engine --cov-report=json:coverage.json --cov-fail-under=100
python tools/check_coverage_ratchet.py coverage.json   # the real gate: 0 missed lines + branch budget
ruff check . --exclude 'tests/fixtures/challenge_mirror'
```

