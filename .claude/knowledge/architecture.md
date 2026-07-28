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

> **CORRECTION (round 25) — wired into production; supersedes the round-24
> correction below (kept for history).** `fetcher/factory.py`'s
> `build_level2_fetcher`/`build_level3_fetcher` now accept a `pool` param;
> `Level2Fetcher`/`Level3Fetcher` lease from it instead of constructing
> `CamoufoxWrapper` directly. **Lifecycle: one pool per rq job, not one per
> process** — `orchestrator/tasks.py::_run_scrape` constructs, `start()`s,
> and `shutdown()`s a `BrowserPool` bracketing a single job's
> `Worker.process_job()` call, because rq forks a fresh "work horse" process
> per job that `os._exit()`s right after (same fact behind the round-24
> tracing `force_flush()` fix below) — a pool literally cannot outlive one
> job. Still a real win for multi-URL-same-domain jobs (crawls), which now
> reuse one hot browser instead of cold-starting Camoufox per URL.
>
> **Mismatch-handling correctness fix (round 25, two parts):**
> 1. `acquire()` used to tear a live browser down on ANY mismatch (wrong
>    domain, wrong proxy) — contradicting this very section's own
>    documented contract ("tear-down only on unhealthy release, idle
>    timeout, or explicit shutdown"). Fixed: a mismatched wrapper is now
>    kept in the pool as a spare for a future request it does match,
>    instead of being destroyed. Total concurrently-alive instances still
>    can't exceed `core.budget.BROWSER_SEMAPHORE` either way.
> 2. A prewarmed wrapper's `_last_domain` starts `None` (never leased yet)
>    — this was being treated as a domain *mismatch* against any real
>    request, evicting every prewarmed instance on its first real use.
>    Fixed: `None` now means "unclaimed," matching anything.
>
> **Proxy mismatch is deliberately NOT given the same "unclaimed matches
> anything" treatment** — see `.claude/knowledge/decisions.md` →
> "BrowserPool Mismatch Handling" for why (a prewarmed browser's proxy is
> baked in at Camoufox launch time and can never be changed; treating
> `proxy=None` as a wildcard would silently serve a proxy-scoped request
> through no proxy at all).
>
> `SessionStateManager` is now actually constructed in production alongside
> each pool (`ttl_days=config.session_retention.browser_sessions_ttl_days`)
> — this is what closes the previously-dead `browser_sessions_ttl_days`
> config field, since the TTL only ever mattered once sessions started
> being persisted for real.

> **CORRECTION (round 24, historical — superseded above).** Everything
> below describes `browser/pool.py::BrowserPool` as designed and as its own
> unit tests exercise it — still accurate. What changed *then*: a fresh
> audit grepped every `BrowserPool(` call site and found zero outside its
> own file and tests — the "hot-browser pool" architecture below was never
> actually wired into the fetch path. Fixed round 25 (above).

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
- Domain/proxy guard: `lease(domain=X, proxy=Y)` only reuses a context whose `_last_domain`/`proxy` matches — except an unclaimed wrapper (`_last_domain is None`, never leased) matches any domain (round 25 fix, see the correction note above). Proxy is never given that same relaxation.
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

**Not integrated with the round-25 Botasaurus path.** `Level2Fetcher`'s
Botasaurus-first attempt (see "Botasaurus Integration" below) does not call
`solve_captcha_on_page` — Botasaurus's Selenium-style `Driver` has no live
Playwright `page` for that pipeline to run against. A challenge-page result
from Botasaurus just falls back to the Camoufox pipeline above, which does
solve captchas normally.

---

## Observability & Tracing (Round 24)

**Single entry point:** `observability/bootstrap.py::bootstrap_observability(cfg)`
— called once per process (api's `create_app()`, `cli/entrypoint.py`'s `main()`,
`proxy/harvester_daemon.py`'s `run()`, and at module import time in
`orchestrator/tasks.py` so it fires once per rq worker process). Calls
`configure_logging()` always, `configure_tracing()` when
`observability.tracing_enabled`.

**Logging:** `observability/logging.py::configure_logging()` bridges stdlib
`logging.getLogger(__name__)` (100% of this codebase's log calls — nothing
calls `get_logger()`) through structlog via `structlog.stdlib.ProcessorFormatter`
on the root logger's handler, rendering every log call (including third-party
libraries like botocore/httpcore) as JSON. `logging_level` per-environment via
`config/{env}.yaml` (`staging.yaml`: DEBUG, `production.yaml`: WARNING).

**Tracing:** `observability/tracing.py::configure_tracing()` builds a real
`TracerProvider` (tagged with `service.name` via `Resource`) exporting to
`observability.otlp_endpoint` (default `http://jaeger:4317` — the `jaeger`
docker-compose service, Jaeger's all-in-one image, native OTLP receiver + UI
on `:16686`, no separate otel-collector). Same function also arms three
process-wide auto-instrumentors — `HTTPXClientInstrumentor`,
`AsyncPGInstrumentor`, `RedisInstrumentor` — so every outbound httpx/Postgres/
Redis call automatically nests as a child span under whatever's currently
active, with zero per-call-site changes. `api/main.py` separately calls
`FastAPIInstrumentor.instrument_app(app)` (needs the `app` object, so it can't
live in `configure_tracing()`) for one span per HTTP request.

**Two manual root spans** give every background code path something for the
auto-instrumented children to nest under:
- `orchestrator/tasks.py::_run_scrape_job` — one `scrape_job` span
  (`job_id`/`tenant_id` attributes) per rq job.
- `proxy/harvester_daemon.py::_run_periodic` — one `proxy_daemon_{name}` span
  per cycle (harvest/promotion/health/retention).

**The one non-obvious gotcha:** rq's work-horse process exits via `os._exit()`
(confirmed in rq's own source), which skips `atexit` entirely, and
`BatchSpanProcessor`'s background export thread doesn't survive `fork()`
either — so `orchestrator/tasks.py`'s job span needs an explicit, timeout-
bounded `force_flush()` in its `finally` block (not just `atexit.register
(provider.shutdown)`, which is what `configure_tracing()` still does for
every *non*-forking process — api, cli, harvester daemon). Full story:
`.claude/knowledge/troubleshooting.md` → "BatchSpanProcessor + fork()".

**Known limitation:** only the API process and the two spots above create
spans. The rq workers' and harvester daemon's *outbound* calls (httpx/pg/
redis) are instrumented and nest correctly under those two root spans, but
nothing else in those processes currently starts its own spans — this is
by design (matches what was asked for), not a gap.

---

## Botasaurus Integration (Round 25)

**What:** `fetcher/botasaurus_wrapper.py::BotasaurusWrapper` — real
Botasaurus fetch, gated by the same `core.budget.BROWSER_SEMAPHORE` as the
Camoufox path (spec §3.6's F-32 fix: Botasaurus's own `@browser(parallel=N)`
manages its own multiprocessing pool internally, so `parallel=1` is always
forced, never caller-configurable — our semaphore stays the single
concurrency authority).

**Where it sits in the escalation:** `Level2Fetcher.fetch()` tries
Botasaurus first (when `fetcher/factory.py` constructed one — gated on
`"botasaurus"` appearing in `config.levels.level_2.engine`), falling back to
the existing full Camoufox pipeline (challenge-detection, captcha-solve,
scroll) on either an exception or a detected challenge page. This split
exists because Botasaurus's driver is Selenium-style with no live Playwright
`page`/`context` — the existing challenge-detection/captcha-solve/scroll
helpers in `fetcher/_content_utils.py` can't run against it. L3 has no
Botasaurus attempt at all (spec: L3 is "Camoufox-only, nuclear").

**Config:** `headless=False` + `enable_xvfb_virtual_display=True` (never
`headless=True` — botasaurus rejects that combination outright, confirmed
live: `ValueError`, and headless is the more easily fingerprinted mode
regardless). `profile=session_id` (`f"{tenant_id}:{domain}"`) — botasaurus's
own persistent-Chrome-profile mechanism, separate from Camoufox's
`storage_state` serialization.

**One-shot per-fetch, opt-in pooled for same-domain jobs (round 26):** a
`BotasaurusWrapper` fetch still launches and tears down its own driver per
call (`reuse_driver=False` — see the pool-safety finding below for why this
stays hardcoded). When `browser/botasaurus_pool.py::BotasaurusPool` is wired
in (opt-in via `Level2Fetcher`'s `botasaurus_pool` param, one instance per
rq job, same lifetime as `BrowserPool`), a 2nd+ fetch for the same
(proxy, domain) within that job reuses the live driver instead.

**History:** this file was deleted as dead code earlier in round 25 (never
imported, `botasaurus` not a declared dependency, and — discovered only
after restoring it — the deleted version called a nonexistent
`driver.page_source` instead of the real `driver.page_html`, so it would
have crashed on its first real fetch even if it had been wired). Restored
and wired for real per an explicit follow-up ask. Full reasoning for both
the deletion and the reversal: `.claude/knowledge/decisions.md` →
"Botasaurus".

### Botasaurus Capability Upgrade (Round 26)

Six capabilities identified by a research pass (`.claude/MEMORY.md` →
Technical Debt, round 25 follow-up) were implemented, every API re-verified
against the real installed source (`pip download botasaurus==4.0.97
botasaurus-driver==4.0.93 botasaurus-requests==4.0.38`, not just the READMEs)
before wiring anything.

**Per-fetch upgrades (`fetcher/botasaurus_wrapper.py`, config-driven via the
new `config.botasaurus: BotasaurusConfig`, `config/schema.py`):**
- `driver.google_get(url, bypass_cloudflare=True)` replaces plain
  `driver.get(url)` — free Cloudflare-tier bypass (Google-referrer spoofing +
  human-like Turnstile solving), no CapSolver spend. Default on.
- `tiny_profile=True` (~1KB vs ~100MB per persisted profile) — **only sent
  when a profile (`session_id`) is actually present.** Verified live: the
  real `botasaurus_driver.core.config.Config.__init__` raises
  `ValueError("Profile must be given when using tiny profile")` if
  `tiny_profile` is set without one — this surfaced as a real crash during
  this round's own live smoke test (a caller with no `session_id`, e.g. an
  ad-hoc/anonymous fetch, would otherwise hard-fail). Same gate applied in
  `BotasaurusPool`.
- `remove_default_browser_check_argument=True`, `close_on_crash=True` —
  concrete anti-detection/reliability `@browser` kwargs, default on.
- `driver.short_random_sleep()` after every load — default on
  (`random_sleep_enabled`).
- `max_retry` — botasaurus's own internal retry+backoff loop; default `0`
  (off, unchanged behavior), only sent to the decorator when `> 0`.
- `UserAgent.HASHED`/`WindowSize.HASHED` (real string constants,
  deterministic per-profile) — only paired with a profile, same gate as
  `tiny_profile`. Never `RANDOM` (botasaurus's own maintainers advise against
  it as a default).

**Same-domain driver reuse (`browser/botasaurus_pool.py::BotasaurusPool`,
item 2 from the research, redesigned):** the original research proposed
botasaurus's own `reuse_driver=True`. Reading
`botasaurus/browser_decorator.py` directly during planning found its
internal `_driver_pool` is a **bare, unkeyed module-level list**
(`.pop()`/`.append()`, no matching on proxy, profile, or tenant at all) —
enabling it as-is would let one tenant's fetch silently receive a driver
still configured with a *different* tenant's proxy/profile, a direct hit on
the tenant-isolation invariant (spec §1.1 #3). `BotasaurusPool` instead
constructs raw `botasaurus.browser.Driver` instances itself (bypassing the
`@browser` decorator and botasaurus's pool entirely) and keys reuse the same
safe way `browser/pool.py::BrowserPool` already keys Camoufox contexts:
proxy + domain match → reuse via `driver.requests.get(url)` (verified: this
runs as an in-page JS `fetch()` through the driver's own tab, so it inherits
that tab's live cookies/TLS session natively — no separate cookie-jar
plumbing needed); mismatch → close the old driver, build a new one. One
instance per rq job, same construction/shutdown bracket as `BrowserPool` in
`orchestrator/tasks.py::_run_scrape`. Wired opt-in through
`Worker`/`fetcher/factory.py::build_level2_fetcher()`/`Level2Fetcher` —
`None` (default off in tests) preserves exactly the pre-round-26 one-shot
behavior.

**L1 JA3 client (`services/botasaurus_requests_client.py`, item 6,
independent of the above):** `botasaurus_requests`' JA3-TLS-fingerprint-
matched `firefox` session, config-gated off by default
(`config.botasaurus.l1_ja3_client_enabled` — a brand-new code path with no
live-traffic validation yet). Wired into `Level1Fetcher` as an optional
first-attempt client, same first-attempt/fallback shape as L2's
Botasaurus-then-Camoufox pipeline. Always calls with `allow_redirects=False`
— `Level1Fetcher` owns the redirect-following loop so every hop still gets
SSRF-revalidated (spec §1.1 #4); letting the client follow redirects
internally would skip that. `botasaurus-requests` was already a transitive
dependency of `botasaurus` but is now declared directly (`pyproject.toml`,
`Dockerfile`, `.github/workflows/test.yml`) since this module imports it
directly — same reasoning as every other direct-import dependency in those
lists (`.claude/knowledge/operations.md` #12).

**Found during PR review, before merge:** the original design called
`self._ja3_client.get(url)` per redirect hop inside `Level1Fetcher.
_fetch_via_ja3`, and each `.get()` constructed a brand-new
`firefox.Session()` — so a cookie set by an intermediate redirect hop (a
common consent/session-redirect pattern) never reached the next hop. Fixed
by adding `BotasaurusRequestsClient.open_session()` → `Ja3Session`, opened
once per top-level `Level1Fetcher.fetch()` call and reused across every hop
of that call's own redirect loop — never shared across separate fetches, so
this doesn't reintroduce the cross-tenant-state class of bug the
`reuse_driver` finding above already ruled out. `BotasaurusRequestsClient.
get()` still exists as a one-shot convenience for callers that don't need
cross-hop continuity.

**Live verification (round 26, corrected):** end-to-end browser verification
against the local `challenge-mirror` container (`http://localhost:8090/`,
confirmed via `/proc/net/tcp` + reading its own `server.py` — it isn't
published on a predictable docker port) initially failed with
`ChromeException: Invalid parameters [code: -32602]` on `Page.navigate`, and
was first misdiagnosed as a Chrome/CDP version mismatch (Chrome 149 vs.
`botasaurus_driver==4.0.93`) — **that diagnosis was wrong.** Isolating the
exact CDP error data (`'Failed to deserialize params.url - ... string value
expected'`) and comparing a raw `Driver().get()` call (worked) against the
`@browser`-decorated path (failed) found the real cause: botasaurus's own
decorator always invokes the wrapped function positionally —
`func(driver, data)` (`browser_decorator.py`'s `run_task`) — where `data` is
`None` for a bare `_fetch()` call. `fetcher/botasaurus_wrapper.py`'s inner
`_fetch(driver, target_url: str = url)` relied on a keyword *default* for
the URL, which that positional call silently clobbers with `None` — so
every fetch was navigating to `None`, not the target URL. Fixed by reading
`url` from the outer closure directly and giving `_fetch` a throwaway
`_data` parameter instead of a same-named default. The existing mocked unit
tests didn't catch this because the test harness's fake decorator called
`fn(_FakeDriver(), URL)` — passing the real URL positionally, unlike
botasaurus's actual `fn(driver, None)` — so it was fixed too (now calls with
`None`, matching production). After the fix: `BotasaurusWrapper.fetch_html()`
against `challenge-mirror` returns real content
(`<h1>Verified Content</h1>`) with `google_get(bypass_cloudflare=True)` and
`short_random_sleep()` both genuinely executing, and a 2-URL same-domain
`BotasaurusPool.fetch()` run confirms exactly one `Driver()` construction
across both calls (the 2nd fetch used `driver.requests.get()`, not a new
browser launch) — both are now real, not just source-cited + mocked.

---

## Metrics: Cross-Process Emission Pattern (Round 25)

**The problem, generalized:** this project runs several distinct process
types — the `api` process (serves `/metrics`), rq worker processes (execute
jobs), the `proxy-harvester` daemon. `prometheus_client`'s `REGISTRY` is
in-process global state. A `Counter`/`Gauge` incremented or set inside a
worker or harvester process is invisible to `/metrics`, because that's a
different process's memory entirely. This is worse than it sounds for rq
specifically: rq forks a brand-new "work horse" process **per job** that
`os._exit()`s immediately after — even a well-intentioned in-process metric
there is gone before the next Prometheus scrape could ever see it. This bit
round 25 twice: the 7 originally-dead alert metrics, and — missed by the
first pass, found only via a live `/metrics` cross-check — `proxy_source_healthy`.

**The fix, applied consistently:** never rely on in-process Prometheus
objects for anything set outside the `api` process. Instead:
1. At event time (inside whichever process the event happens in), write a
   plain value to Redis (`redis.raw.set`/`incr`) or query Postgres directly.
2. At scrape time (inside `api/routes.py`'s `/metrics` handler, the *only*
   process that matters here), read that Redis/Postgres state back and set
   the local `Gauge` right before `generate_latest(REGISTRY)` runs.

This is not a new pattern invented in round 25 — `observability/metrics.py`'s
original `proxy_pool_validated_count` already worked this way (a live
Postgres `COUNT(*)` query at scrape time). Round 25 just applied it
everywhere a metric's event and its scrape don't share a process:
`dlq_size`, `capsolver_daily_spend`/`capsolver_daily_ceiling`,
`circuit_breaker_trips_total`, `proxy_exhausted_total`,
`job_duration_seconds_count`/`_sum`, `proxy_source_healthy`. The one
exception is `http_requests_total` — a normal in-process `Counter`, because
HTTP requests and the `/metrics` scrape both happen in the same long-lived
`api` process; no cross-process problem to work around there.

**When adding a new metric:** ask first which process the event happens in.
If it's not the `api` process, this pattern is required — see
`observability/metrics.py`'s `refresh_*` functions for the exact shape to
copy. Also see `.claude/knowledge/standards.md` → "Prometheus Gauges".

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
