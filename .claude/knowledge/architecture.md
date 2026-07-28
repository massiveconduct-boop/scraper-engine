# Architecture

**Purpose:** System design, invariants, module interactions, data flow.
**Scope:** Complete system architecture. Does NOT duplicate the specification — references it.
**When to read:** Understanding how components connect; adding new modules; debugging cross-cutting concerns.
**Related:** `specs/scraper-engine-blueprint-v2.md`, `.claude/knowledge/decisions.md`

---

## Design Invariants (from spec §1.1 — non-negotiable)

1. No component calls proxybroker2 HTTP control API — all proxy state in Postgres/Redis.
2. Camoufox owns 100% of fingerprint/UA/canvas/WebGL surface — app code never touches.
3. `tenant_id` is explicit `TenantId` value object everywhere — no ambient ContextVar at trust boundaries.
4. Every outbound fetch is SSRF-checked before enqueue and after every redirect.
5. Nothing cached as success unless `FetchResult.success is True` and not challenge page.
6. Every resource acquisition has guaranteed release path — context manager or TTL, never both.
7. SQL identifiers validated against allow-list regex before interpolation.

---

## Escalation State Machine

```
PENDING → CIRCUIT_CHECK → FETCHING_L1 → PARSING_L1
                                      ↘ failure → ESCALATING_L2 → FETCHING_L2 → PARSING_L2
                                                                               ↘ failure → ESCALATING_L3 → FETCHING_L3
                                                                                                              ↘ failure → DEAD_LETTER
Non-retryable (SSRF, quota, proxy exhausted): direct → DEAD_LETTER
```

Levels: L1 (httpx/Scrapling, timeout 20s, any proxy), L2 (Botasaurus+Camoufox, timeout 40s, anonymous+ proxy), L3 (Camoufox-only, timeout 60s, elite proxy).

---

## Proxy Pipeline

```
harvest_once()
  ├─ _direct_scrape()        [PRIMARY — 5-10 proxies in ~5s]
  │   ├─ 8 source URLs → _scrape_one() per source
  │   │   ├─ _parse_ip_port() / _parse_geonode()
  │   │   ├─ _tcp_probe() (2s timeout)
  │   │   └─ _http_validate() through self-hosted judge (:8089)
  │   │       └─ Score: TCP-only=25 (below L1), validated=60 (above L1)
  │   └─ Persist to proxy_pool with anonymity_level + reliability_score
  │
  └─ _harvest_via_broker()   [SUPPLEMENTARY — 1-5 validated in ~20s]
      └─ proxybroker2 subprocess (30s timeout)
          └─ broker.find() → validate → JSON stdout → persist
```

**Self-hosted judge:** `judge_server.py` on port 8089. Echoes headers + origin. Replaces httpbin.org dependency.

**Source diversity:** 8 URLs across 6 operators (proxyscrape.com, openproxylist.xyz, TheSpeedX/GitHub, monosans/GitHub, pubproxy.com, geonode.com). 5 real failure domains (GitHub CDN shared by two repos).

**Scoring:** Two-tier. TCP-only=25 (below L1 threshold 40 — cannot be selected). HTTP-validated=60. `promote_tcp_only()` background job re-validates TCP-only proxies.

---

## Browser Pool

**Design:** Hot-browser pool with real reuse. `pool.start(N)` launches N Camoufox instances and stores live contexts in an asyncio.Queue. `pool.lease(proxy, domain)` is the async context manager — returns a live context, guarantees release (structural cleanup per invariant §1.1.6).

**Key methods:**
- `start()` — launches prewarm_count browsers, stores (context, wrapper, idle_since)
- `acquire(domain)` — classifies drained items as selected/keep/teardown per idle timeout + domain matching
- `release(ctx, healthy)` — healthy returns to pool, unhealthy tears down
- `lease(proxy, domain)` — async context manager wrapping acquire/release
- `shutdown()` — tears down all live contexts

**Session persistence (round 7):** When `session_mgr` is supplied, storage_state is loaded in `acquire()` (outside the classify-loop), passed through `CamoufoxWrapper.__init__(storage_state=...)`, applied in `__aenter__` via `browser.new_context(storage_state=blob)` (Path B — Path A unavailable, AsyncCamoufox does not forward the kwarg). State saved back to Postgres on healthy `lease()` exit. Save failures logged at WARNING with `exc_info=True` — pool continues serving.

**Safety properties:**
- Semaphore-gated: `BROWSER_SEMAPHORE` prevents unbounded spawn (F-14).
- No double-issue: `acquire()` classifies each item exactly once — selected item never re-queued.
- Process cleanup: `__aexit__` always runs, browser process reaped.
- Domain guard: `lease(domain=X)` only reuses context whose `_last_domain` matches.
- Session I/O outside classify-loop: load at line 125, classify-loop return at line 119. Session code never executes during queue bookkeeping.

---

## PgBouncer

**Architecture:** `pgbouncer-init` Docker service auto-regenerates SCRAM userlist from Postgres `pg_authid.rolpassword`. PgBouncer mounts shared volume. Zero manual steps.

**Transaction pooling:** `PostgresClient.acquire()` wraps SET search_path in `BEGIN...COMMIT` to ensure all statements hit the same backend connection.

---

## API Routing (Round 8-11 — Fully Wired)

All routes enforce 4 invariants per blueprint:

1. **Tenant/auth** — `TenantResolver.resolve(api_key)` via `X-API-Key` header → `TenantId`. 401 on bad key.
2. **SSRF guard** — `SSRFGuard.validate(url)` on every URL before processing. 403 on blocked ranges.
3. **Quota enforcement** — `QuotaManager.check_and_increment(tenant_id)` reads per-tenant limit from `public.tenants.quota_daily_limit`. Raises `QuotaExceededError` → 429. No bare except.
4. **DB persistence** — `INSERT INTO scrape_jobs` before returning. `GET /v1/jobs/{job_id}` queries live `scrape_jobs` table. 404 on missing.

**Startup:** `api/main.py` uses `lifespan` context manager to initialize `PostgresClient`, `RedisClient`, and `TenantResolver` singletons. `@app.on_event("startup")` was unreliable in FastAPI 0.139.2.

---

## SSRF Enforcement — Two Checkpoints, Not One (Round 22 — closes invariant #4 TOCTOU gap)

Invariant #4 ("every outbound fetch SSRF-checked before enqueue and after every
redirect") was only half-true through round 21: `SSRFGuard.validate()` ran
once in `api/routes.py` at job-submission time and never again. The actual
fetch happens later, in a different process (the worker), after a queue wait
of unknown length — a DNS-rebind or same-request redirect to a private/
metadata address in that window bypassed the guard completely.
`validate_redirect_chain()` existed but had zero production call sites
(grep-verified) despite docs claiming it was wired.

Now enforced at both checkpoints:
1. **Submit time** (unchanged) — `api/routes.py`, before enqueue.
2. **Fetch time** (new, round 22) — every fetcher re-validates immediately
   before connecting, and again per redirect hop:
   - `Level1Fetcher` (httpx): `follow_redirects=True` replaced with a manual
     redirect loop, validating each hop before following it.
   - `Level2Fetcher`/`Level3Fetcher` (Camoufox/Playwright): a `page.route()`
     handler (`fetcher/_content_utils.py::SSRFRouteGuard`) validates every
     request/redirect at the browser layer, aborting blocked ones and
     surfacing the real `SSRFBlockedError` (Playwright itself only reports a
     generic aborted-request error).

Also fixed in `core/ssrf_guard.py`: `_resolve_host` checked only the first
`getaddrinfo()` result, so a multi-record DNS answer (public IP first,
private second) could slip through — renamed to `_resolve_hosts`, checks
every resolved address.

Live-proven inside the real `worker-l2` container: a genuine private-network
navigation attempt (docker-network IP, `172.18.0.13`) was correctly blocked.
Full evidence: `.claude/knowledge/decisions.md`.

---

## Fetcher Construction & Shared Content Helpers (Round 13-16)

- **DI factory:** `fetcher/factory.py::build_level1/2/3_fetcher(config)` is the ONLY
  production path to a fetcher (CI grep-gate enforces it). Reads `config.levels.level_N`
  (unified `LevelConfig`: goto/networkidle/max_total/post_load/retry_increment/scroll fields).
- **Shared helpers** (`fetcher/_content_utils.py`, used by L2 + L3):
  `safe_content` (mid-nav guard), `poll_until_solved` (ChallengeDetector-gated retry),
  `autoscroll` (lazy-load/infinite-scroll, consecutive-stable stop).
- **`fetcher/_failure.py::classify_fetch_exception`** maps DNS errors → HOST_UNREACHABLE.
- **Escalation additions:** worker escalates JS-gated L1 shells (`looks_javascript_gated`)
  and dead-letters HOST_UNREACHABLE immediately (no futile L1→L2→L3).

## CAPTCHA Solving (Round 19 provider layer, Round 20 fetch-path wiring)

Provider-abstracted, primary-with-fallback, **wired into the L2/L3 fetch path**
(round 20 — was provider-only in round 19).

```
CaptchaSolver(primary=NoCaptchaAI, fallback=CapSolver)   [services/captcha_solver.py]
  ├─ solve_recaptcha_v2 / solve_turnstile / solve_hcaptcha / solve_aws_waf
  │  / solve_geetest / solve_mtcaptcha  → try primary, on None → fallback
  └─ build_captcha_solver(budget)  ← env keys (NOCAPTCHA_AI_API_KEY, CAPSOLVER_API_KEY)

services/_anticaptcha.py   — shared createTask/getTaskResult (arbitrary task dict),
                              solve_image_to_text (OCR, sync), get_balance
services/nocaptcha.py      — NoCaptchaAIClient (primary)  [provider-specific task types]
services/capsolver.py      — CapSolverClient (fallback; also covers hCaptcha)
```

Both gated by `CapSolverBudget` (per-tenant $/day, BD-03) + `CAPSOLVER_CONCURRENCY`.
Task-type strings are provider-specific and were live-corrected from stale docs
(see troubleshooting.md).

**Fetch-path wiring (round 20):** the worker builds the solver once
(`build_captcha_solver(CapSolverBudget(redis))`) and threads it through the
factory into L2/L3. After `poll_until_solved`, if the page still classifies as a
challenge, `Level*Fetcher._maybe_solve_captcha` calls
`fetcher/_captcha.solve_captcha_on_page`:

```
solve_captcha_on_page(page, solver, tenant_id, url)      [fetcher/_captcha.py]
  detect widget (page.evaluate → {kind, sitekey})  # recaptcha_v2 | hcaptcha | turnstile
    → solver.solve_<kind>(tenant, sitekey, url)  → token
    → inject token (kind-specific JS; recaptcha also fires ___grecaptcha_cfg callback)
    → caller waits, re-polls; ChallengeDetector still gates success
```

Best-effort: returns False (never raises) on no widget / no sitekey / no token /
inject failure → degrades to "still a challenge", never a false positive. Null-safe:
no provider key → solver is None → fetch runs with solving skipped. Observable via
`captcha_solve_attempts_total{kind}` / `captcha_solved_total{kind}`. DOM detect/inject
is unit-tested with a fake page; live-verified end to end round 22 (real Camoufox
+ real DOM detect + real inject — see `docs/round-20-evidence.md` for the
mechanics). The one thing NOT proven live is a target actually *accepting* a
solved token, because no NoCaptchaAI solve has produced a token yet on this
account — root-caused round 22 as an account-side gap (no subscription plan,
not a code bug); see `.claude/knowledge/troubleshooting.md` → "Captcha task
accepted but stuck idle forever" and `decisions.md` for the full evidence.

---

## Data Flow

```
API Client → FastAPI (/v1/scrape) → TenantResolver → SSRFGuard → QuotaManager → DB Insert → RQ Queue → Worker
                                                ├─ CircuitBreaker.allow_request()
                                                ├─ PolitenessController.acquire_slot()
                                                ├─ ProxyManager.get_proxy() → ProxyHarvester.harvest_once()
                                                ├─ Level1/2/3Fetcher.fetch() → FetchResult
                                                └─ DedupEngine → PostgresClient → proxy_pool
```
