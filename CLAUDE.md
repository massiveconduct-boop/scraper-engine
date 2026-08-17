# Scraper Engine — CLAUDE.md

Project identity, operating rules, and navigation. Currently at round 57 (root-caused and fixed the browser-error-page bug a
peer Claude session flagged last round: `botasaurus_driver.Driver.get()`/
`google_get()` wrap a raw CDP `Page.navigate` and never inspect or raise on
a network-level navigation failure (DNS, connection reset, empty response,
proxy failure) — confirmed by reading the actual installed
`botasaurus_driver` source, not inferred from wording — so Chromium
silently renders its own `chrome-error://chromewebdata/` interstitial as
if it were a real page, and every downstream safety net
(`ChallengeDetector`'s vendor signatures, gateway-error regex, Firefox-
plaintext-wrapper regex) was individually verified to structurally miss
it, since none of them expect Chromium's own UI chrome as input. Confirmed
NOT affected, by reading each: L1 (pure httpx/JA3/Scrapling, real status
codes and real exceptions throughout), L2's Camoufox fallback and all of
L3 (Playwright's `page.goto()` DOES raise for real network failures,
already correctly caught), and `botasaurus_pool.py`'s driver-reuse path
(uses an in-page JS `fetch()` that already raises `DriverException`).
Fixed by reusing the existing, already-correct `BROWSER_CRASH` failure
pipeline instead of adding a new category: new
`browser/_botasaurus_nav_check.py::raise_if_navigation_failed()` checks
`driver.current_url` for Chromium's internal error scheme right after
navigation in the 2 confirmed gap sites (`botasaurus_wrapper.py`,
`botasaurus_pool.py`), raising `BotasaurusNavigationError` — a plain
`Exception` automatically caught by the existing `except (Exception,
SystemExit): return None` in `level_2.py::_fetch_via_botasaurus` (the same
handler round 40 added for `SystemExit`), with zero `level_2.py`/
`worker.py` changes needed; the Botasaurus→Camoufox fallback now actually
triggers instead of silently persisting garbage as `success=True`. Second,
independent defense-in-depth layer: a structural, locale-independent
`net::ERR_` regex added to `ChallengeDetector`, plugging into `worker.py`'s
already-centralized `is_challenge_page` classification for free. Live-
verified via 12 new tests (using the peer's actual 3 reported example
bodies as fixtures) — full gate: 964 passed (up from round 56's 952 by
exactly the new tests), 3 skipped (pre-existing, unchanged), 0 failed,
100.00% coverage. Not live-browser-verified this session (would need a
real Botasaurus/Camoufox launch against a deliberately-broken network
target) — flagged as the one open item, not blocking. Full detail:
technical-debt.md round-57 entry. Round 56 (rounds 51-55 — `dlq_reaper` cross-category starvation
fix, two orphaned-PENDING-job reconciliation/live-deploy rounds, an
enqueue-failure-after-insert fix, and a PROCESSING-stuck-forever rq-hard-kill
fix — not narrated in this rolling paragraph, see technical-debt.md's
round-51 through round-55 entries; this paragraph's rolling summary resumes
below at round 50). Round 56 (user asked what capabilities were already
built but never exposed to callers like `research_agent` via API/CLI.
Audited and found 5 real gaps, all capability-already-exists-just-not-routed:
no job-list endpoint, no quota-visibility endpoint (`QuotaManager.remaining()`
existed since early rounds, unused by any route — callers only learned
their limit by hitting a 429), no tenant-wide DLQ listing
(`DeadLetterQueue.list_for_tenant()`'s `job_id=None` mode already existed
for ops tooling/the `dlq_size` gauge, just had no route), no caller-facing
webhook-event-schema reference (`webhook_events.py`'s taxonomy was
internal-only), and an ops-only CLI with no caller-facing HTTP client.
Implemented all 5, split into 4 independently-shipped, purely additive
phases per explicit user instruction not to bundle them: Phase A `GET
/v1/jobs` (paginated, tenant-scoped, optional status filter) + `GET
/v1/quota`; Phase B `GET /v1/dlq` (tenant-wide); Phase C `GET
/v1/webhook-events` (static reflection of `WebhookEventType` +
`WebhookEvent.model_json_schema()`); Phase D new `scraper-engine api` CLI
subcommand group (`scrape`/`jobs`/`job`/`quota`/`dlq`) — a thin `httpx`
client over the real HTTP API, unlike every other CLI command which talks
directly to Postgres/Redis, giving a curl-free path through the same
auth/SSRF/quota checks a real integrator hits. Caught and fixed one real
bug along the way: FastAPI's `Query(50, ge=1, le=500)` parameter marker is
never resolved to its plain value when a route function is called directly
— which is how every test in `test_api_routes.py` calls routes — fixed
with plain `int` params plus a new shared `_validate_pagination()` helper
instead of `Query()`. Zero existing route/function/SQL/response-model was
modified — every change is additive. Live-verified: full gate rerun with
real docker-compose infra (postgres/redis/pgbouncer) after all 4 phases —
952 passed, 3 skipped (pre-existing Camoufox/CAPTCHA live-test skips), 0
failed, 100.00% coverage. Separately, a peer Claude session working on
`research_agent` flagged via cross-session message a distinct, NOT YET
INVESTIGATED bug this round: a real fraction of scrape results come back
`success=True` with `content` that's actually a rendered browser/proxy-level
error page (Chromium DNS_PROBE/connection-reset, `ERR_EMPTY_RESPONSE`, a
proxy-layer "No internet" page) rather than real page content — distinct
from the already-understood real-404-content case (round 45). Suspected,
unconfirmed root cause: `ChallengeDetector` likely doesn't cover Chromium's
own internal error-page UI, which probably renders `http_status=200` with
no known challenge signature. Explicitly deferred to a future round per
user instruction — logged as an open thread, not investigated or fixed
this round. Full detail: technical-debt.md round-56 entry. Round 50 (same peer session, follow-up report with concrete evidence: `fingerprint_preset=True` (round 46) crashed an ENTIRE job — `BrowserPool.start()`'s prewarm loop runs outside `process_job`'s per-URL try/except — when a randomly-sampled real fingerprint's WebGL vendor/renderer isn't covered by camoufox's own separate `webgl_data.db` lookup table (`ValueError: No WebGL data found for vendor...`, verified against the actual installed camoufox source: `camoufox/utils.py` passes a preset's pinned vendor/renderer straight to `camoufox/webgl/sample.py::sample_webgl`, which raises if that exact pair isn't a row in the table — a genuine gap between camoufox's own two internal datasets). Three distinct real vendor/renderer combos observed crashing across two runs, confirming a real percentage of the fingerprint pool, not a rare edge case. Fixed with the same pattern as round 37's `InvalidIP` geoip fallback: `CamoufoxWrapper._launch_with_geoip_fallback()` retries with `fingerprint_preset=False` on this specific, message-scoped `ValueError`, restructured into a bounded 3-attempt loop so it can stack with the geoip fallback in one launch. Full detail: technical-debt.md round-50 entry. Round 49 (a peer Claude session working on `research_agent`, a sibling service hitting this API over HTTP, reported real batches scoring 0-7/33, dominated by `detection_block`/`circuit_open`/`scraper_engine_job_timeout`. Root-caused two real gaps: `free_first` (round 40) only fell back to the paid gateway on total pool exhaustion, never on a circuit-open domain or a still-blocked final-level result — the two failure modes actually being hit; and `process_job`'s zero-concurrency URL loop (round 45, deliberately deferred until "fix everything reported" made it in scope) made large batches slow enough to trip the caller's own job-timeout. Fixed both: `FetchResult.proxy_source` + a `force_gateway` param let `process_job` force a level through the gateway when the circuit is open (level 1 skipped — no gateway path there — straight to level 2) or when a final-level result is still blocked after one retry; `process_job`'s URL loop is now `asyncio.Semaphore`-bounded concurrent dispatch (new `politeness.max_concurrent_urls_per_job`, default 5), verified safe since PolitenessController/CircuitBreaker are already Redis-atomic per-domain and BROWSER_SEMAPHORE already caps live browsers process-wide. Full detail: technical-debt.md round-49 entry. Round 48 (user asked what else in the codebase needed round 47's fix — audited the rest of `config/base.yaml` for the same "hardcoded literal, no env override" pattern. Found and fixed 2 real matches: `levels.level_2/level_3.capsolver_enabled` (gated real CapSolver spend, hardcoded `true`, now a shared `${CAPSOLVER_ENABLED:true}`) and `botasaurus.l1_ja3_client_enabled` (a real opt-in feature, hardcoded `false`, now `${BOTASAURUS_L1_JA3_CLIENT_ENABLED:false}`); both live-verified via `load_config()`. Deliberately left the rest of `base.yaml` (circuit breaker, politeness, proxy-tier fallback, pgbouncer, session retention, dlq_reaper, observability toggles) as internal ops-tuning knobs, not capability toggles — converting those would add real misconfiguration risk for a source-blind external caller without matching round 47/48's actual bug pattern. Full detail: technical-debt.md round-48 entry. Round 47 (a developer on `research_agent`, a separate service that calls this one over HTTP, reported it couldn't turn on the DataImpulse paid-gateway proxy — no bind-mounted source, no visibility into this repo's `docker-compose.yml`, so it can't edit code or compose files directly, even though DataImpulse credentials were already reaching the container via env. Root cause: `dataimpulse.enabled`/`strategy` in `config/base.yaml` were the only hardcoded, non-overridable literal values in the whole config file — every other setting already used the `${VAR:default}` env-placeholder pattern. Fixed: both now read `${DATAIMPULSE_ENABLED:false}`/`${DATAIMPULSE_STRATEGY:free_only}`; live-verified via `load_config()` that unset env keeps the unchanged default (`enabled=False`) and setting the env vars actually flips it, no rebuild needed. `.env.example` also gained a full DataImpulse section — it previously documented none of these vars at all. Full detail: technical-debt.md round-47 entry. Round 46 (user asked whether "detection_block" failures meant we weren't using the full anti-detection features of Scrapling/Botasaurus/Camoufox. Found a real bug on investigation: 3 of 12 failures were a FALSE POSITIVE in our own `ChallengeDetector` — bare `"interstitial"`/`"g-recaptcha"` signatures matching completely normal Google Ad Manager ad-slot code and a comment-form reCAPTCHA widget on real, live 200-status pages, not actual anti-bot pages; removed both, live-verified all 3 URLs now succeed. Separately upgraded Camoufox's anti-detection config, verified against real docs (Context7): added `fingerprint_preset=True` (real captured fingerprints, officially recommended for our Firefox 152) and `os="linux"` (pinned to match the actual Docker host — Camoufox's own docs warn impersonating a different OS is counterproductive, not an improvement). Live-verified this doesn't flip `crunchbase.com`/`cbn.gov.ng`'s remaining genuine 403 blocks — concluded these are IP-reputation-based (proxy quality), not fixable via browser fingerprint tuning; the only real lever is round 40's opt-in paid gateway proxy. Full detail: technical-debt.md round-46 entry. Round 45 (user pushed back with real evidence — their own browser loaded nairametrics.com/sec.gov.ng fine, contradicting round 44's "404 = definitively dead" assumption — requesting deeper investigation, suspecting anti-bot detection. Correct: live-verified 2 of 5 domains' "404" was actually Cloudflare bot-management rejecting L1's non-JS request ("error code: 1010"), disguised as not-found. Round 44's NOT_FOUND (permanent, no escalation) was too confident — reverted: 404 now added to `ChallengeDetector.CHALLENGE_STATUS_CODES` alongside 403/429/5xx, so it escalates through a real browser like any other block status; only if the FINAL level's own real-browser render STILL looks blocked does `worker.py` downgrade it to a real failure (closing a separate pre-existing gap where the final level unconditionally accepted "whatever it got," which is exactly how a real 404 error page for businessday.ng had been silently stored as "successful" markdown). Live-verified against the exact 7 URLs in question: `sec.gov.ng`, `techcabal.com`, `konga.com` now correctly succeed (real 200 via browser escalation — confirms the user's suspicion); `nairametrics.com` (both URLs), `punchng.com`, `businessday.ng`'s specific article paths independently confirmed genuinely dead via two separate methods (system's real L3 Camoufox browser, and a no-proxy realistic-header direct check both hitting the real origin's own WordPress 404 page) — each domain's homepage verified healthy, only these specific stale deep-links are gone. Full detail: technical-debt.md round-45 entry. Round 44 (root-caused all 6 remaining failures from round 43's rerun, user-requested "robust and resilient solutions": a definitive HTTP 404 (`FailureCategory.NOT_FOUND`, new) now stops escalation and is exempt from circuit-breaker penalty instead of wasting L2/L3 attempts and damaging domain health over a URL that will never exist; `classify_fetch_exception`'s marker-based DNS-failure matching was mislabeling PROXY-side DNS blips as permanent `HOST_UNREACHABLE` — since SSRFGuard's own unproxied pre-check already proves a domain resolves before any level's real fetch attempt runs, a raw DNS exception surfacing after that can only be proxy/network-side, so it now falls through to the caller's already-retryable default; circuit breaker's `max_cooldown_seconds` cut 3600s→1200s and `trip_count` now TTL-decays instead of compounding forever (both live-verified: `crunchbase.com`/`cowrywise.com`, blocked for hours by stale trip state, succeeded immediately once cleared — they were never actually unscrapeable). Full detail: technical-debt.md round-44 entry. Round 43 (live rerun of round 42's fix surfaced 4 more issues, user-requested "root-cause and fix, one at a time, live verify each": (1) markdown RecursionError fallback made to actually convert deeply-nested real pages instead of just degrading — iterative wrapper-chain flattening plus a large-stack thread for genuinely deep nesting; (2) SSRF guard was mislabeling dead/unresolvable domains as `ssrf_blocked` instead of the already-existing `HOST_UNREACHABLE` category — fixed via `SSRFBlockedError.is_unresolvable`; (3) circuit breaker had no TTL on its failure-streak counters, so one job's crashed-run failures silently poisoned later unrelated jobs' trip decisions for up to an hour — fixed with `failure_streak_ttl_seconds`, plus a `record_success` counter-reset asymmetry; (4) remaining genuine `proxy_exhausted` cases verified NOT a bug — real free-tier top-tier supply scarcity, already correctly labeled and mitigated. Full detail: technical-debt.md round-43 entry). Round 42 root-caused "proxy_exhausted" to ground truth, user-requested — two stacked bugs, neither about proxy supply: `orchestrator/worker.py::process_job`'s terminal escalation branch was fabricating `PROXY_EXHAUSTED` for any all-levels-failed reason, which was hiding a real schema regression — migrations 004/005/007 had each silently reverted migration 002's `browser_sessions` fix, breaking session-state persistence on 100% of live tenant schemas with `column "storage_state" does not exist`; fixed with a real-category-preserving terminal branch plus new migration `008_fix_browser_sessions_schema_regression.py`, live-verified both together. Round 41 root-caused and fixed round 40's Xvfb display-contention crash — botasaurus_driver's non-atomic Xvfb display-number picker plus a leftover-lock-file leak on close; fixed via a new process-wide `core.budget.XVFB_LOCK` serializing display spinup/teardown across both engines, plus proactive stale-file cleanup; live-verified crash-free across 4 rounds of concurrent real jobs. Round 40 added a toggleable paid rotating-gateway proxy (DataImpulse) for L2/L3, additive to the free pool, off by default. Round 39 corrected years of silently-inflated proxy scoring plus four leasing-reliability hardenings. Round 38 grew free-harvest source breadth and fixed two L3-scoring bugs. Round 37: six-layer L2/L3 leasing-reliability fix. Full round-by-round narrative, every open thread, every decision: `.claude/knowledge/technical-debt.md` (start there, not here — this file is navigation only). Source code is fully implemented — not blueprint phase.

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
| `browser/` | CamoufoxWrapper (now takes `geoip`/`humanize`/`headless_mode`, round 25; round 37 — `_launch_with_geoip_fallback()` retries the browser launch once with `geoip=False`, same proxy, if Camoufox's own internal geoip IP-lookup raises `InvalidIP`, since that's an unrelated third-party dependency failing, not the proxy itself being unusable; round 40 — passes proxy credentials through to Camoufox/Playwright's native `proxy=` option, see architecture.md; round 41 — `__aenter__`/`__aexit__` hold `core.budget.XVFB_LOCK` across launch/close), session state. `pool.py::BrowserPool` (hot-browser `lease()`) is wired into L2/L3 as of round 25 — one pool per rq job (see `.claude/knowledge/architecture.md` → "Browser Pool"). `botasaurus_pool.py::BotasaurusPool` (round 26) — same one-per-rq-job shape, reuses one live Botasaurus driver across same-domain URLs; round 40 — embeds proxy credentials in its Driver `proxy` string; round 41 — launch+evict-close both under `XVFB_LOCK`, plus `_close_driver` now calls the new `_xvfb_cleanup.py::cleanup_stale_display()`. `_xvfb_cleanup.py` (round 41 — new, removes a just-closed Botasaurus driver's leftover Xvfb lock/socket files that `pyvirtualdisplay`'s own `Display.stop()` SIGKILL leaves behind). `_botasaurus_nav_check.py` (round 57 — new, `raise_if_navigation_failed()` checks `driver.current_url` for Chromium's own `chrome-error://` scheme right after `driver.get()`/`google_get()` in both `botasaurus_pool.py::_new_driver_fetch` and `fetcher/botasaurus_wrapper.py`, raising `BotasaurusNavigationError` — closes a real silent-false-success gap, see technical-debt.md round-57 entry) |
| `fetcher/` | Level1/2/3 fetchers, `factory.py` (DI, CI-gated), `_content_utils` (shared guard/poll/scroll), `challenge_detector`, `_failure`, `_captcha.py` (DOM detect→solve→inject→re-poll, round 20), `botasaurus_wrapper.py` (round 25 — real Botasaurus fetch attempt tried before the Camoufox pipeline in L2; capability-upgraded round 26, see `.claude/knowledge/architecture.md` → "Botasaurus Integration" / "Botasaurus Capability Upgrade"; round 40 — embeds proxy credentials, same as `botasaurus_pool.py`; round 41 — `fetch_html()` holds `core.budget.XVFB_LOCK` for its whole call, since Botasaurus's `@browser` decorator bundles launch+navigate+close with no seam to release the lock earlier; cleans up the launched Driver's stale Xvfb files via `_xvfb_cleanup.py` afterward), `level_2.py` (round 40 — Botasaurus→Camoufox fallback now also catches `SystemExit`, see troubleshooting.md; round 57 — that same catch now also correctly triggers on `browser/_botasaurus_nav_check.py::BotasaurusNavigationError`, no `level_2.py` change needed since it's a plain `Exception` subclass), `challenge_detector.py` (round 57 — new unconditional `_CHROMIUM_NET_ERROR_RE` structural check, defense-in-depth for Chromium's own internal network-error interstitial, same tier as the existing gateway-error/Firefox-wrapper checks), `scrapling_wrapper.py` (round 28 — L1's third first-attempt engine, gated on `config.levels.level_1.engine == "scrapling"`), `adaptive_selector.py` (round 28 — structured extraction, called from `Worker.process_job`, not from within `fetcher/`). `level_1.py` no longer touches Firecrawl at all (round 29 — moved to `orchestrator/worker.py`, see below) |
| `orchestrator/` | Worker (escalation state machine — round 29 adds a `CACHE_TTL_DAYS`-gated cache-reuse check per URL, a cooperative `_is_cancelled` mid-loop check, and centralizes markdown conversion here instead of L1; round 34 adds `PERMANENT_FAILURE_CATEGORIES`/`TRANSIENT_FAILURE_CATEGORIES` DLQ split and `partial_failure` derivation; round 37 — `_fetch_with_proxy()` shared L2/L3 lease-fetch-score helper gains one same-level retry with a fresh proxy when a fetch fails with a proxy-attributable category (`BROWSER_CRASH`/`NETWORK_TIMEOUT`), since even a preflighted proxy can still fail Camoufox's own internal geoip IP-lookup at launch; round 40 — fails fast on a misconfigured paid-gateway toggle, `_fetch_with_proxy()` branches on `config.dataimpulse.strategy`, see `.claude/knowledge/architecture.md` → "Paid Gateway Proxy"; round 42 — `process_job`'s terminal for/else branch now reports the real last-level `failure_category`/`error_message` via a new `last_level_result` tracker instead of fabricating `PROXY_EXHAUSTED`/"All fetch levels exhausted" for any all-levels-failed reason, see architecture.md → "proxy_exhausted Mislabeling + browser_sessions Schema Regression"), CircuitBreaker, PolitenessController, `job_queue.py` (rq producer), `tasks.py` (rq consumer entry point — round 29: `_persist_one_result` persists each result as it lands via `Worker.process_job`'s `on_result` callback instead of batching at the end; round 34: webhook dispatch now goes through the durable outbox, `_persist_one_result` also clears stale DLQ entries on success), `WebhookDispatcher` (round 34 — config-driven retry/timeout, accepts pre-rendered payloads not just `JobStatusResponse`), `webhook_events.py`/`webhook_dispatch.py`/`webhook_sweeper.py`/`slack_formatter.py` (round 34 — event taxonomy, durable outbox writer, standalone retry-sweeper daemon, Slack Block Kit rendering; see `.claude/knowledge/architecture.md` → "Notifications & Proxy Self-Healing") |
| `api/` | FastAPI routes (wired: SSRF guard, tenant auth, per-tenant quota, DB persist, rq enqueue, composite health check). Round 29 adds `Idempotency-Key` dedup on `/v1/scrape`+`/v1/crawl` (before the quota charge), `GET /v1/jobs/{job_id}/dlq`, and `DELETE /v1/jobs/{job_id}` (cancellation). Round 34: the `webhook` field is now SSRF-guarded too, same checkpoint as scrape target URLs. Round 36: `/v1/health` also reports per-daemon liveness (`daemons` field, `health.py::_check_daemon_liveness`) — informational only, doesn't affect the HTTP status code. Round 56 adds `GET /v1/jobs` (tenant-scoped, paginated, optional status filter — new `JobSummaryResponse` in `core/models.py`), `GET /v1/quota` (surfaces `QuotaManager.remaining()`/`current_usage()`, previously unrouted), `GET /v1/dlq` (tenant-wide dead-letter listing, the sibling of the existing job-scoped `GET /v1/jobs/{job_id}/dlq`), and `GET /v1/webhook-events` (static `WebhookEventType`/`WebhookEvent` schema reflection); all four are purely additive, share the existing `_validate_uuid`-style auth/503/401 boilerplate, and use a new `_validate_pagination()` helper (plain `int` params, not FastAPI's `Query(...)` marker — that marker is never resolved when a route function is called directly, which is how this file's whole test suite calls routes). Middleware |
| `storage/` | PostgresClient (BEGIN...COMMIT PgBouncer isolation), RedisClient, S3Client (round 29 — `SUCCESS_RETENTION_DAYS` bumped 1→7 to match the new cache window), DLQ (round 29 — `list_for_tenant` takes an optional `job_id` filter; round 34 — UPSERTs on `(job_id, url)`, `retry()` removed in favor of `mark_retry_attempt`/`clear`, gained `auto_retry_count`), `webhook_outbox.py` (round 34 — transactional outbox mirroring DLQ's shape) |
| `config/` | Pydantic schema, YAML loader |
| `cli/` | Entrypoint. Every subcommand through round 55 talks directly to Postgres/Redis (ops tooling: `serve`/`worker`/`harvest`/`reap`/`check`/`create-tenant`). Round 56 adds a caller-facing `api` subcommand group (`scrape`/`jobs`/`job`/`quota`/`dlq`) — a thin synchronous `httpx` client wrapping the real `/v1` HTTP API instead of touching storage directly, so a dev can smoke-test or script against the same auth/SSRF/quota path a real integrator uses, curl-free. `--base-url`/`--api-key` (env fallback `SCRAPER_ENGINE_API_KEY`). `cli/` is outside `pyproject.toml`'s `[tool.coverage.run].source`, so it isn't gated by the 100% coverage requirement, but still has its own test file (`tests/unit/test_cli_entrypoint.py`, added round 56 — none existed before). |
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

