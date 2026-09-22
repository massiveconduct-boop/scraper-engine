# Scraper Engine — CLAUDE.md

Project identity, operating rules, and navigation. Currently at round 66.
This paragraph is deliberately a one-liner, not a round-by-round diary —
a round-28 knowledge-architecture audit removed 7 growing dated paragraphs
from this exact spot once already (see "Evolution history" below); the
pattern crept back in over rounds 37-57 and a round-57 knowledge audit
removed it again (see `decisions.md` → "Knowledge-Audit: Round-57 CLAUDE.md
Diary Regression"). If you're about to prepend a new round's narrative
here, it belongs in `.claude/knowledge/technical-debt.md` and, if terse,
the "Evolution history" bullet below — not here. Full round-by-round
narrative, every open thread, every decision: `.claude/knowledge/
technical-debt.md` (start there, not here — this file is navigation
only). Source code is fully implemented — not blueprint phase.

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
- **Testing:** pytest 9.1.1, unit+integration+chaos suite + 18 live + load suite (counts: see CI, not hardcoded here per operating rule #4). Captcha/Camoufox live tests skipped in CI (no Camoufox binary there). **Coverage gate (round 62, widened round 64): the real gate is `tools/check_coverage_ratchet.py` — zero missed LINES (no tolerance) plus an absolute missed-BRANCH budget that may only shrink, now at 0. `fail_under` is back at 100 as a backstop (it was 99 while branches were short of 100%); the ratchet is still the authoritative gate.** Gate covers 10 packages (core, proxy, orchestrator, fetcher, services, storage, api, scrapy_project, cli, observability); `browser/` is measured-but-ungated (needs a real Firefox) and `config/` is outside it — see `.claude/knowledge/technical-debt.md` round-62 coverage audit and round 64. Two former local-environment exclusions were closed in round 35, both root-caused as sandbox/venv corruption rather than real gaps (a wrong-arch native `.so` accepted by an upstream `check_library()` bug, and wrong-arch `playwright`/Camoufox binaries) — full mechanism in `.claude/knowledge/technical-debt.md`'s round-35 entry. The chaos tests also need `tests/fixtures/challenge_mirror`'s server running locally (`python -m app.server`, port 8090), which isn't started automatically. A round-34 knowledge audit caught and same-day-closed a regression to 97.91%; see `.claude/knowledge/technical-debt.md` round-34 entry, "Coverage gap" note, for detail.
- **Linting:** ruff (clean), mypy `--strict` clean (baseline retired round 18)
- **Evolution history (one clause per round; a round-28 audit moved the
  full narrative out of this file once, a round-57 audit re-trimmed a
  regrowth, and this list itself was re-trimmed again — see decisions.md
  → "STATUS.md Stale Zero-Concurrency Claim" for the latest audit):**
  execution pipeline + SSRF hardening (22), observability/tracing (24),
  BrowserPool/CapSolver/Botasaurus wiring (25), Botasaurus capability
  upgrade (26), src/ layout consolidation (27), coverage gate + dead-code
  wiring (28), caller-experience gap closure + caching (29), proxy
  self-healing + notification rewrite + DLQ auto-retry (34, rounds 30-33
  not backfilled), proxy_exhausted root-cause fix via ASN classification
  (35), `/v1/health` daemon-liveness checks (36), L2/L3 proxy-leasing
  reliability fix (37), free-harvest-source growth + 2 scoring bugs fixed
  (38), proxy-scoring correction + leasing hardening (39), toggleable
  paid gateway proxy (40), Xvfb display-contention fix (41),
  `proxy_exhausted` root-caused to a schema regression (42), 4 issues
  from a live rerun (43), failure-category + circuit-breaker fixes (44),
  404 reverted to escalate through a real browser (45), ChallengeDetector
  + fingerprint pinning fixes (46), config toggles made env-overridable
  (47-48), gateway-fallback gaps fixed + concurrent URL dispatch shipped
  (49), fingerprint WebGL-crash fix (50), DLQ/orphaned-job/enqueue
  reliability fixes (51-55), 4 unrouted capabilities surfaced as API/CLI
  phases (56), Botasaurus silent-false-success fix (57), Botasaurus
  autoscroll fix (58), `block_images` + RAM-aware concurrency cap (59),
  Botasaurus extensions/lang/locale/timezone/mouse/network-capture (60),
  CAPTCHA no-active-plan check wired into the real solve path + politeness
  slot-retry fix (61), paid-gateway exit-IP rotation + measured ASN pin +
  startup/compose self-healing + stuck-job reaper reachability fix +
  branch-coverage ratchet (62), bulk-crawl throughput from a second
  consumer report — per-domain level memory, politeness rewrite, a
  live-job-reaping stuck-job reaper, a browser-pool permit deadlock, the
  100-link cap, L2 driver-reuse determinism and per-phase job timing (63),
  a remaining-issues sweep — L2 lost to proxy blocks (gateway retry at every
  level) and hid why (`escalations`), a cross-engine browser-permit
  protocol, Botasaurus display-lock scope, an L1 redirect loop reported as
  success, crawl SSRF on redirects + proxied crawls, required-deps 503s,
  level-hint lifetime, per-domain skip-the-pool / skip-Botasaurus hints,
  and a 0-branch coverage gate widened to
  `cli`/`observability`/`scrapy_project` (64), host-wide browser admission
  (seat + politeness slot claimed together, pressure-sized, off by default)
  plus per-slot politeness expiry and re-drive fixes (65), a proxy's 407
  as its own `proxy_auth_failed` category — terminal on the paid gateway,
  re-driven only after a probe through it succeeds (66).
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
| `core/` | Domain models, TenantId, SSRF guard, retry, budget, quota. `periodic.py` — shared polling-loop helper (with optional Redis liveness heartbeat) reused by the webhook sweeper and DLQ reaper. `budget.py` — `XVFB_LOCK` (process-wide lock serializing headful-browser display spinup/teardown), `resolve_browser_max_total_instances()` (opt-in RAM-aware ceiling on live browser instances) and `acquire_browser_permit()` — the one way ANY engine takes a `BROWSER_SEMAPHORE` permit: reclaims parked instances via registered pools and counts waiters, so a pool hands a returning instance's permit over instead of parking it. `models.py` — `Proxy` (paid-gateway auth fields), `FetchResult` (`network_events` from Botasaurus CDP capture). `startup.py` — `wait_for_dependency()`, the unbounded-by-default dependency wait every process's startup uses instead of exiting when Postgres/Redis/S3 isn't up yet. `host_identity.py` — which physical host a process is on (`SCRAPER_HOST_ID`, else kernel `boot_id`) |
| `proxy/` | Harvester (multi-source + broker subprocess), Manager (leasing, TCP+HTTPS-CONNECT preflight, SQL-side candidate exclusion, exhaustion wakes the harvester), `net_probe.py` (shared probe primitives), Scoring, Lease, `asn_classifier.py` (reverse-DNS ASN classification), `health_monitor.py` (rolling re-validation + rescoring), `pool_health.py` (per-tier HEALTHY/DEGRADED/CRITICAL state machine), `dlq_reaper.py` (transient-DLQ auto-retry daemon), `paid_gateway.py` (toggleable DataImpulse gateway proxy; renders the provider's username grammar — per-attempt `sessid` rotation for a fresh exit IP, optional `asn` pin, `cr` country), `retention_reaper.py` |
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

