# Scraper Engine — CLAUDE.md

Project identity, operating rules, and navigation. Currently at round 43 (live rerun of round 42's fix surfaced 4 more issues, user-requested "root-cause and fix, one at a time, live verify each": (1) markdown RecursionError fallback made to actually convert deeply-nested real pages instead of just degrading — iterative wrapper-chain flattening plus a large-stack thread for genuinely deep nesting; (2) SSRF guard was mislabeling dead/unresolvable domains as `ssrf_blocked` instead of the already-existing `HOST_UNREACHABLE` category — fixed via `SSRFBlockedError.is_unresolvable`; (3) circuit breaker had no TTL on its failure-streak counters, so one job's crashed-run failures silently poisoned later unrelated jobs' trip decisions for up to an hour — fixed with `failure_streak_ttl_seconds`, plus a `record_success` counter-reset asymmetry; (4) remaining genuine `proxy_exhausted` cases verified NOT a bug — real free-tier top-tier supply scarcity, already correctly labeled and mitigated. Full detail: technical-debt.md round-43 entry). Round 42 root-caused "proxy_exhausted" to ground truth, user-requested — two stacked bugs, neither about proxy supply: `orchestrator/worker.py::process_job`'s terminal escalation branch was fabricating `PROXY_EXHAUSTED` for any all-levels-failed reason, which was hiding a real schema regression — migrations 004/005/007 had each silently reverted migration 002's `browser_sessions` fix, breaking session-state persistence on 100% of live tenant schemas with `column "storage_state" does not exist`; fixed with a real-category-preserving terminal branch plus new migration `008_fix_browser_sessions_schema_regression.py`, live-verified both together. Round 41 root-caused and fixed round 40's Xvfb display-contention crash — botasaurus_driver's non-atomic Xvfb display-number picker plus a leftover-lock-file leak on close; fixed via a new process-wide `core.budget.XVFB_LOCK` serializing display spinup/teardown across both engines, plus proactive stale-file cleanup; live-verified crash-free across 4 rounds of concurrent real jobs. Round 40 added a toggleable paid rotating-gateway proxy (DataImpulse) for L2/L3, additive to the free pool, off by default. Round 39 corrected years of silently-inflated proxy scoring plus four leasing-reliability hardenings. Round 38 grew free-harvest source breadth and fixed two L3-scoring bugs. Round 37: six-layer L2/L3 leasing-reliability fix. Full round-by-round narrative, every open thread, every decision: `.claude/knowledge/technical-debt.md` (start there, not here — this file is navigation only). Source code is fully implemented — not blueprint phase.

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
  consolidated into one supervised container (round 35), `/v1/health`
  extended with informational daemon-liveness checks (round 36), six-layer
  L2/L3 proxy-leasing reliability fix — TCP+HTTPS-CONNECT preflight
  (closes a 150s+ job-hang regression round 35's scoring fix exposed),
  SQL-side candidate exclusion, one same-level retry with a fresh proxy on
  proxy-attributable fetch failures, Camoufox geoip-launch fallback, and a
  fix to round 34's DLQ auto-retry eligibility check that had been
  silently disabled for a free-proxy-only deployment (round 37), grew free
  harvest source breadth 8→12, fixed an early-break bug that had silently
  starved 2 of the original 8 sources for the pool's entire lifetime, then
  fixed the two real scoring bugs behind the long-assumed "free sources
  structurally can't reach L3" ceiling — a judge-validation latency
  measurement polluted by dead-candidate timeout time, and a proxy's
  latency reading frozen forever at its first sample instead of refreshed
  by later health checks (round 38). Current design, topic-
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
| `core/` | Domain models, TenantId, SSRF guard, retry, budget, quota, `periodic.py` (round 34 — shared `run_periodic` loop helper, extracted from `proxy/harvester_daemon.py` so `orchestrator/webhook_sweeper.py` and `proxy/dlq_reaper.py` reuse it instead of re-implementing; round 36 — optionally writes a Redis liveness heartbeat after every cycle attempt when a `redis` client is passed; round 40 — `models.py::Proxy` gained auth fields for the paid-gateway proxy, see architecture.md → "Paid Gateway Proxy"), `budget.py` (round 41 — new `XVFB_LOCK`, a process-wide `asyncio.Lock` serializing every headfull-browser Xvfb spinup/teardown across both Botasaurus and Camoufox, closing round 40's open display-contention crash — see architecture.md → "Xvfb Display-Contention Lock") |
| `proxy/` | Harvester (multi-source + broker subprocess; round 38 — `_http_validate` now times each judge candidate individually and returns `(is_valid, anonymity, latency_ms)`, latency reflecting only the winning request instead of wall-clock around the whole multi-judge loop, since dead-candidate timeout time was previously counted as the proxy's own latency and was crushing scores pool-wide), Manager (round 34 — exhaustion now sets a debounced Redis kick key + publishes, waking `harvester_daemon.py`'s fast-poll watcher instead of waiting on the timer; round 37 — TCP+HTTPS-CONNECT preflight via `net_probe.py::lease_preflight` right before leasing a candidate, treating a probe failure like a real fetch failure (`mark_failure`) so a dead-but-promoted proxy fails in low single-digit seconds instead of costing the caller a full 40-60s browser navigation timeout; `_select_candidate`'s query also gained SQL-side exclusion of already-tried candidates, not just Python-side filtering of a fixed top-20), `net_probe.py` (round 37 — shared `tcp_probe()`/`http_probe()`/`lease_preflight()`; `tcp_probe` extracted out of `ProxyHarvester._tcp_probe` so both the harvester's candidate pre-filter and Manager's lease-time preflight use one implementation; `http_probe` deliberately checks an HTTPS URL, not plain HTTP — a proxy can forward plain HTTP fine but fail HTTPS CONNECT tunneling, which is what real target pages and Camoufox's own geoip launch check actually need), Scoring, Lease, `asn_classifier.py` (round 35 — real ASN classification via reverse-DNS PTR hostname lookup, no external account/db needed, replaced the never-actually-wired MaxMind path; unconditional, no env gate), `health_monitor.py` (round 38 — its existing rolling re-validation cycle now actually rescores a proxy on a passing check instead of just bumping `last_validated`: `check_one()` delegates to `ProxyHarvester._http_validate` + a new ASN classify() call, and `reliability_score` is recomputed via `ScoringEngine` folding in real accumulated success/failure counts — previously a proxy's latency/anonymity/ASN reading was captured once at harvest time and never refreshed, permanently capping its score regardless of real-world performance; concurrency bounded to `HEALTH_CHECK_CONCURRENCY=5`, matching `promotion.py`'s existing semaphore pattern, after the added classify() call made a fully-sequential 100-row cycle take 15-25+ minutes against its 300s interval), `pool_health.py` (round 34 — per-tier HEALTHY/DEGRADED/CRITICAL state machine, drives both the ops-Slack path and DLQ auto-retry eligibility), `dlq_reaper.py` (round 34 — standalone daemon, auto-retries transient DLQ entries once their condition clears; round 35 — runs as a supervised process inside the `api` container, not its own compose service; round 37 — `_is_eligible()`'s `PROXY_EXHAUSTED` check now follows `allow_tier2_fallback_for_tier3`, checking tier 2's health for a level-3-exhaustion entry instead of tier 3's, since tier 3's raw health had read CRITICAL on this deployment — round 38 found that wasn't a structural free-source limit but two scoring bugs, both now fixed, so tier 3 health may stop being reliably CRITICAL over time; this dlq_reaper fix stays correct either way; round 42 — its own `_TRANSIENT_CATEGORIES` list, deliberately separate from `orchestrator/worker.py`'s `TRANSIENT_FAILURE_CATEGORIES`, now also covers `BROWSER_CRASH`/`NETWORK_TIMEOUT` with the same tier-health eligibility check as `PROXY_EXHAUSTED`), `paid_gateway.py` (round 40 — new, builds the toggleable DataImpulse gateway proxy, see architecture.md), `retention_reaper.py` (its `browser_sessions` cleanup was silently no-oping against every tenant schema until round 42's migration fixed the table it targets — see architecture.md → "proxy_exhausted Mislabeling + browser_sessions Schema Regression"). Manager also gained round 39 leasing-reliability fixes (tier-1 fallback, `MAX_ATTEMPTS` 5→10, track-record-first ordering, `ban_domain` param) — full detail: decisions.md → "Round 39 Leasing-Reliability Hardening" |
| `browser/` | CamoufoxWrapper (now takes `geoip`/`humanize`/`headless_mode`, round 25; round 37 — `_launch_with_geoip_fallback()` retries the browser launch once with `geoip=False`, same proxy, if Camoufox's own internal geoip IP-lookup raises `InvalidIP`, since that's an unrelated third-party dependency failing, not the proxy itself being unusable; round 40 — passes proxy credentials through to Camoufox/Playwright's native `proxy=` option, see architecture.md; round 41 — `__aenter__`/`__aexit__` hold `core.budget.XVFB_LOCK` across launch/close), session state. `pool.py::BrowserPool` (hot-browser `lease()`) is wired into L2/L3 as of round 25 — one pool per rq job (see `.claude/knowledge/architecture.md` → "Browser Pool"). `botasaurus_pool.py::BotasaurusPool` (round 26) — same one-per-rq-job shape, reuses one live Botasaurus driver across same-domain URLs; round 40 — embeds proxy credentials in its Driver `proxy` string; round 41 — launch+evict-close both under `XVFB_LOCK`, plus `_close_driver` now calls the new `_xvfb_cleanup.py::cleanup_stale_display()`. `_xvfb_cleanup.py` (round 41 — new, removes a just-closed Botasaurus driver's leftover Xvfb lock/socket files that `pyvirtualdisplay`'s own `Display.stop()` SIGKILL leaves behind) |
| `fetcher/` | Level1/2/3 fetchers, `factory.py` (DI, CI-gated), `_content_utils` (shared guard/poll/scroll), `challenge_detector`, `_failure`, `_captcha.py` (DOM detect→solve→inject→re-poll, round 20), `botasaurus_wrapper.py` (round 25 — real Botasaurus fetch attempt tried before the Camoufox pipeline in L2; capability-upgraded round 26, see `.claude/knowledge/architecture.md` → "Botasaurus Integration" / "Botasaurus Capability Upgrade"; round 40 — embeds proxy credentials, same as `botasaurus_pool.py`; round 41 — `fetch_html()` holds `core.budget.XVFB_LOCK` for its whole call, since Botasaurus's `@browser` decorator bundles launch+navigate+close with no seam to release the lock earlier; cleans up the launched Driver's stale Xvfb files via `_xvfb_cleanup.py` afterward), `level_2.py` (round 40 — Botasaurus→Camoufox fallback now also catches `SystemExit`, see troubleshooting.md), `scrapling_wrapper.py` (round 28 — L1's third first-attempt engine, gated on `config.levels.level_1.engine == "scrapling"`), `adaptive_selector.py` (round 28 — structured extraction, called from `Worker.process_job`, not from within `fetcher/`). `level_1.py` no longer touches Firecrawl at all (round 29 — moved to `orchestrator/worker.py`, see below) |
| `orchestrator/` | Worker (escalation state machine — round 29 adds a `CACHE_TTL_DAYS`-gated cache-reuse check per URL, a cooperative `_is_cancelled` mid-loop check, and centralizes markdown conversion here instead of L1; round 34 adds `PERMANENT_FAILURE_CATEGORIES`/`TRANSIENT_FAILURE_CATEGORIES` DLQ split and `partial_failure` derivation; round 37 — `_fetch_with_proxy()` shared L2/L3 lease-fetch-score helper gains one same-level retry with a fresh proxy when a fetch fails with a proxy-attributable category (`BROWSER_CRASH`/`NETWORK_TIMEOUT`), since even a preflighted proxy can still fail Camoufox's own internal geoip IP-lookup at launch; round 40 — fails fast on a misconfigured paid-gateway toggle, `_fetch_with_proxy()` branches on `config.dataimpulse.strategy`, see `.claude/knowledge/architecture.md` → "Paid Gateway Proxy"; round 42 — `process_job`'s terminal for/else branch now reports the real last-level `failure_category`/`error_message` via a new `last_level_result` tracker instead of fabricating `PROXY_EXHAUSTED`/"All fetch levels exhausted" for any all-levels-failed reason, see architecture.md → "proxy_exhausted Mislabeling + browser_sessions Schema Regression"), CircuitBreaker, PolitenessController, `job_queue.py` (rq producer), `tasks.py` (rq consumer entry point — round 29: `_persist_one_result` persists each result as it lands via `Worker.process_job`'s `on_result` callback instead of batching at the end; round 34: webhook dispatch now goes through the durable outbox, `_persist_one_result` also clears stale DLQ entries on success), `WebhookDispatcher` (round 34 — config-driven retry/timeout, accepts pre-rendered payloads not just `JobStatusResponse`), `webhook_events.py`/`webhook_dispatch.py`/`webhook_sweeper.py`/`slack_formatter.py` (round 34 — event taxonomy, durable outbox writer, standalone retry-sweeper daemon, Slack Block Kit rendering; see `.claude/knowledge/architecture.md` → "Notifications & Proxy Self-Healing") |
| `api/` | FastAPI routes (wired: SSRF guard, tenant auth, per-tenant quota, DB persist, rq enqueue, composite health check). Round 29 adds `Idempotency-Key` dedup on `/v1/scrape`+`/v1/crawl` (before the quota charge), `GET /v1/jobs/{job_id}/dlq`, and `DELETE /v1/jobs/{job_id}` (cancellation). Round 34: the `webhook` field is now SSRF-guarded too, same checkpoint as scrape target URLs. Round 36: `/v1/health` also reports per-daemon liveness (`daemons` field, `health.py::_check_daemon_liveness`) — informational only, doesn't affect the HTTP status code. Middleware |
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

