# Design Decisions

**Purpose:** Record WHY decisions were made, TRADEOFFS considered, ALTERNATIVES rejected.
**Scope:** Irreversible or high-cost decisions. Routine implementation choices excluded.
**When to read:** Before changing architecture; when a decision seems wrong and needs context.
**Related:** `.local/specs/scraper-engine-blueprint-v2.md` (local-only, not tracked in git), `.claude/knowledge/architecture.md`

---

## Decision: proxybroker2 Subprocess Isolation

**Date:** 2026-07-24 | **Round:** 4-5

**What:** proxybroker2 runs in isolated `asyncio.create_subprocess_exec()` with venv Python, returning JSON via stdout. Not imported in-process.

**Why:** proxybroker2 uses aiohttp; harvester imports httpx. Combined imports caused source flakiness (with vs without httpx produced different proxy counts in early tests). Later disproved as the root cause (both return 3 proxies in same-script test), but subprocess isolation remains as defense-in-depth.

**Tradeoffs:** Adds ~0.5s subprocess overhead per harvest cycle. Guarantees no aiohttp/httpx event loop conflict regardless of diagnosis.

**Alternatives:** In-process import of proxybroker2 (rejected — unpredictable event loop behavior). Direct scraping only (rejected — proxybroker2 provides judge-validated proxies).

**Status:** Active. Subprocess isolation is defense-in-depth, not root cause fix.

---

## Decision: Two-Tier Proxy Scoring

**Date:** 2026-07-24 | **Round:** 6

**What:** TCP-probe-only proxies scored at 25 (below L1 threshold 40). HTTP-validated proxies scored at 60.

**Why:** Free proxies have ~0.02% HTTP forwarding success rate. TCP probe catches 96% of dead ones cheaply (connection refused = instant reject). Full HTTP validation catches the remaining 4% but takes 5s each. Two-tier ensures pool is never ~99.6% dead weight while still identifying the rare working HTTP proxies.

**Tradeoffs:** TCP-only proxies are quarantined (cannot be selected by ProxyManager). `promote_tcp_only()` background job re-validates them. Until promoted, pool relies on broker-validated proxies (score 60) or occasional HTTP-validated direct-scrape proxies.

**Alternatives:** Validate everything through HTTP (rejected — impossible to get 40+ proxies in reasonable time). Validate nothing, accept dead pool (rejected — blueprint §2).

**Status:** Active.

---

## Decision: `lease()` Async Context Manager

**Date:** 2026-07-24 | **Round:** 6

**What:** `pool.lease(proxy, domain)` wraps acquire/release in try/finally. Callers use `async with pool.lease() as ctx:`.

**Why:** Hot-browser pool rewrite (`acquire()` returning bare context) broke invariant §1.1.6 — context cleanup was no longer guaranteed on exception. `lease()` restores the structural guarantee: release ALWAYS runs, healthy on normal exit, unhealthy (teardown) on exception.

**Tradeoffs:** Adds one level of indirection. acquire() and release() remain public (should be prefixed `_acquire`/`_release` — deferred).

**Alternatives:** Restore CamoufoxWrapper as return type (rejected — wrapper's __aexit__ always tears down, can't support healthy re-queue).

**Status:** Active. Lease is the contract. acquire/release are internal.

---

## Decision: PgBouncer Auto-Entrypoint

**Date:** 2026-07-24 | **Round:** 5-6

**What:** `pgbouncer-init` Docker service queries Postgres for SCRAM verifier, writes userlist.txt to shared volume. PgBouncer mounts shared volume. Zero manual steps.

**Why:** edoburu/pgbouncer Docker image auto-generates MD5 userlist. Postgres 16 requires SCRAM-SHA-256. MD5→SCRAM mismatch causes "wrong password type" on every connect. Dynamic SCRAM regeneration from `pg_authid.rolpassword` solves authentication permanently.

**Tradeoffs:** Adds one init container. Requires pg_hba.conf rule `host all all 172.0.0.0/8 md5` for PgBouncer→Postgres forwarding on Docker bridge network.

**Alternatives:** Static userlist.txt file (rejected — breaks when Postgres container is recreated with new SCRAM salt). auth_query (rejected — chicken-and-egg: PgBouncer needs auth to query for auth).

**Status:** Active.

---

## Decision: `acquire()` Classify-Once Pattern

**Date:** 2026-07-24 | **Round:** 6

**What:** Every item drained from pool is classified exactly once: selected, kept, or torn down. Only `keep` goes back into `self._pool`.

**Why:** Prior implementation re-queued ALL items to pool before selecting one. Selected item stayed in queue — second acquire() handed same live context to two callers. Classify-once prevents double-issue structurally, not via caller discipline.

**Tradeoffs:** More complex than simple queue.get_nowait(). But prevents a class of concurrency bugs that caller discipline cannot reliably prevent.

**Alternatives:** Remove and re-queue separately (rejected — the bug this decision fixes). Lock around acquire/release (rejected — overkill for async Python; pool is already single-threaded via asyncio).

**Status:** Active. Regression tests (`TestAcquireDoubleIssue`) catch reoccurrence.

---

## Decision: Free Proxy Sources Only

**Date:** 2026-07-24 | **Round:** 6

**What:** 6 operators accepted as permanent ceiling. Blueprint's "50+ sources" language retired.

**Why:** 50+ independently-operated free proxy sources do not meaningfully exist. Chasing the number had diminishing returns. 5-6 sources across 5 failure domains is the real-world ceiling for free-tier sourcing.

**Tradeoffs:** Fewer sources = less resilience to any single source going dark. Mitigated by `ProxyPoolCriticallyLow` alert firing on validated count <5.

**Alternatives:** Paid proxy services (rejected by product owner — "free only" constraint). Building proprietary proxy scraper with 50+ websites (rejected — maintenance burden exceeds benefit).

**Status:** Active. Product owner decision.

---

## Decision: PostgreSQL 16 with SCRAM-SHA-256

**Date:** 2026-07-24 | **Round:** 5

**What:** PostgreSQL 16 enforces SCRAM-SHA-256 for host connections. PgBouncer must use matching auth_type.

**Why:** Postgres 16 changed default password encryption from md5 to scram-sha-256. The edoburu/pgbouncer image only generates md5 hashes. Dynamic SCRAM regeneration solves the mismatch.

**Status:** Active. `infra/pgbouncer/userlist.txt` auto-regenerated by pgbouncer-init.

---

## Decision: Session Persistence via Postgres, Not Redis

**Date:** 2026-07-25 | **Round:** 7-8

**What:** `SessionStateManager` persists Camoufox `storage_state` blobs to per-tenant `browser_sessions` Postgres table (domain-keyed, 30-day TTL). Session state loaded in `acquire()` via `CamoufoxWrapper` constructor, saved on healthy `lease()` exit. Session I/O structurally outside the classify-loop in `acquire()`.

**Why:** Original implementation used Redis. Switched to Postgres for tenant-scoped isolation (reuses `PostgresClient.acquire(tenant_id)` with per-tenant schemas, same pattern as the rest of the storage layer). Redis had no per-tenant key isolation. Postgres also provides expiry management (`expires_at > NOW()` query, `browser_sessions` index on `expires_at`).

**Tradeoffs:** Postgres queries on every domain-miss acquire (session load) and healthy lease exit (session save). Acceptable overhead relative to Camoufox launch time (~80MB RSS, 2-4s cold start). Session save failures logged at WARNING with `exc_info=True` — pool continues serving, state lost on next recycle.

**Alternatives:** Redis with tenant-prefixed keys (rejected — diverges from storage layer's single-connection pattern). Playwright's native `storage_state` passed to `AsyncCamoufox` constructor (rejected — Camoufox does not forward `storage_state` to Playwright context creation; Path B: `browser.new_context(storage_state=blob)` is the only correct approach).

**Status:** Active. Full round-trip verified: cookie write → storage_state() → Postgres save → warm context eviction → Postgres load → CamoufoxWrapper(storage_state=...) → new_context(storage_state=...) → cookie present.

---

## Decision: Exception-Based Quota Enforcement with Per-Tenant Limits

**Date:** 2026-07-25 | **Round:** 8-11

**What:** `QuotaManager.check_and_increment()` raises `QuotaExceededError` on limit hit — never returns bool. The route handler catches only that specific exception → 429. No bare `except Exception: pass`. Per-tenant limit read from `public.tenants.quota_daily_limit` column, falling back to `DEFAULT_DAILY_LIMIT = 10_000`.

**Why:** The original stub code called `check_and_increment()` as fire-and-forget wrapped in `except Exception: pass`, masking a `TypeError` (nonexistent `pg=` kwarg) on every request. The bool-return assumption in the designer's directive was incorrect — `check_and_increment` never returned bool. Exception-based enforcement with specific catch is explicit and auditable. Per-tenant limits via DB column replace hardcoded global cap.

**Tradeoffs:** Redis stores quota counters (`quota:daily:{date}:{tenant_id}`). `public.tenants` is a global table, not tenant-scoped — quota limit is an identity/config attribute, same pattern as `public.api_keys`. Redis failure (connection refused) → quota enforcement skipped → quota is advisory, not hard-gating. This is an explicit tradeoff: availability over strict quota enforcement.

**Alternatives:** PostgreSQL-based quota counters (rejected — Redis Lua scripts provide atomic increment-and-check without race conditions). Global-only limit (rejected — two-tenant isolation test caught cross-tenant Redis key collision; fixed by including `tenant_id` in the key).

**Status:** Active. Two-tenant curl evidence: system (limit=2) → 200, 200, 429; other (limit=5) → 200×5, 429.

---

## Decision: mypy Ratchet Gate for CI — Baseline-Based Regression Prevention

**Date:** 2026-07-26 | **Round:** 10

**What:** CI lint job runs `mypy ... --ignore-missing-imports` and diffs findings against a committed `tools/mypy-baseline.txt` (23 known findings). Any *new* error beyond the baseline fails the build (`exit 1`). Known findings are advisory (reported but not blocking). The baseline shrinks over time via PR discipline: any PR touching a file in the baseline must resolve that file's entries.

**Why:** The codebase is not mypy-clean. `--strict` on GitHub's runner produces 23 findings across 6 files (different stub resolution than local). Shipping `--strict` as a blocking gate would permanently redline CI. `|| true` (advisory-only) provides zero protection against regression. The ratchet protects against *new* type errors while the known set shrinks.

**Tradeoffs:** Local and CI produce different finding counts (10 vs 23) — same config file (`pyproject.toml`, `strict = true`), different stub resolution (pydantic/starlette versions). Baseline must be CI-specific. Updating the baseline requires deliberate action (not automated).

**Alternatives:** `--strict` blocking gate (rejected — CI permanently red). `|| true` advisory-only (rejected — no regression protection). Per-file `# type: ignore` suppression (rejected — hides errors without documenting them).

**Status:** Active. Proven on real CI: deliberate probe file caught by ratchet, build failed, probe reverted, CI green again. Run URL: https://github.com/massiveconduct-boop/scraper-engine/actions/runs/30189977872.

---

## Decision: L2/L3 page.content() Race — Network-Idle + Config-Driven Wait Strategy

**Date:** 2026-07-26 | **Round:** 10

**What:** `Level2Fetcher` uses `wait_until="domcontentloaded"` + bounded `networkidle` (5s timeout) before calling `page.content()`. `Level3Fetcher` uses `wait_until="load"` + fixed 10s post-load delay. Timeout values driven by `config/production.yaml` (`levels.level_2.*`, `levels.level_3.*`), not hardcoded in the fetcher.

**Why:** The original `page.goto()` with no `wait_until` parameter (defaults to `"load"`) called `page.content()` while client-side PoW JavaScript was still executing. Produced `Page.content: Unable to retrieve content because the page is navigating and changing the content.` Level 2's standard challenges are network-activity-driven → `networkidle` detects completion. Level 3's strict challenges are CPU-bound PoW with no network I/O → `networkidle` cannot detect JS execution state → fixed post-load delay is the correct strategy for this specific challenge type.

**Tradeoffs:** L3's fixed 10s delay is generous against the self-hosted mirror (~8-12s PoW) but could be too short for very slow real targets. `max_total_wait_ms: 30000` provides ceiling. The `"challenge-mirror-ok"` string in test assertions is a fixture marker — production challenge detection uses `ChallengeDetector` class, not string matching.

**Alternatives:** Hardcoded sleeps in both fetchers (rejected — already caused the bug; config-driven makes tuning auditable). Polling loop for content (rejected — excessive `page.content()` calls slow the page). Single strategy for both levels (rejected — L2 and L3 challenge types require different detection strategies).

**Status:** Active. L2 4.66s PASSED, L3 14.75s PASSED against self-hosted mirror.

---

## Decision: Fetcher Construction via DI Factory + CI Gate

**Date:** 2026-07-26 | **Round:** 13

**What:** All production fetcher construction goes through `fetcher/factory.py`
(`build_level1/2/3_fetcher(config)`), never bare `LevelN Fetcher()`. A grep-based
CI gate in `.github/workflows/test.yml` fails if any non-test file constructs a
fetcher directly.

**Why:** Round 12.2 made fetchers config-driven, but every call site had to
remember to pass config — a silent-drift risk (a new worker/refactor forgetting
config would fall back to constructor defaults with no error). The factory is the
single place production.yaml is guaranteed authoritative.

**Tradeoffs:** Tests are exempt (mock-arg construction is normal). The gate is a
grep, not import analysis — cheap, same pattern as the `_debug` endpoint and
mypy ratchet gates.

**Alternatives:** DI container (rejected — overkill); trust discipline (rejected —
this project has been bitten by "works when someone remembers").

**Status:** Active. Gate proven by deliberate-violation test.

---

## Decision: Shared Fetch-Content Helpers — One Source of Truth

**Date:** 2026-07-26 | **Round:** 14-16

**What:** `fetcher/_content_utils.py` holds the guard + poll + scroll logic used by
both L2 and L3: `safe_content(page)` (mid-navigation `page.content()` guard,
increments `safe_content_none_total`), `poll_until_solved(...)` (ChallengeDetector-
gated bounded retry), `autoscroll(...)` (lazy-load/infinite-scroll). L3's private
`_safe_content` was removed in favour of the shared version.

**Why:** L2 had a ~timing-race flake (networkidle fired before a PoW POST/redirect;
a single `page.content()` read grabbed the unsolved interstitial). L3 had already
solved this exact bug class. Rather than copy the logic a second time, it lives
once — same principle applied to ChallengeDetector in round 12.3.

**Tradeoffs:** `page` is duck-typed `Any` (helpers don't hard-import Playwright
types, which aren't consistently importable across the Camoufox stack).

**Discovery (round 16):** autoscroll must stop only after N *consecutive* flat
passes (default 2), not the first — AJAX-loaded content lags the scroll (observed
live on quotes.toscrape.com/scroll: 10→20, flat, →30). Stopping on the first flat
pass abandons content mid-load.

**Status:** Active. L2: 20/20 isolation + 6/6 under load. Scroll: live-proven 10→30 quotes.

---

## Decision: HOST_UNREACHABLE Non-Retryable + JS-Gated Escalation

**Date:** 2026-07-26 | **Round:** 15

**What:** (1) DNS/unresolvable-host errors classify as `FailureCategory.HOST_UNREACHABLE`
(non-retryable in the matrix; worker dead-letters immediately, no L1→L2→L3 escalation).
(2) An L1 200 that is a JS-gated SPA shell (`ChallengeDetector.looks_javascript_gated`)
escalates instead of being cached as content.

**Why:** Surfaced by real-target validation. A dead domain was categorised
`BROWSER_CRASH` (retryable) → wasted up to 6 browser launches on a host that can
never resolve. A JS-only SPA returned 200 with an empty mount point and was
accepted as "content" — real data never fetched.

**Tradeoffs:** `looks_javascript_gated` is deliberately conservative (JS-required
marker OR empty SPA mount AND thin visible text) to avoid escalating full static
pages that merely carry a `<noscript>` tag.

**Status:** Active. Regression-tested in test_worker.py + test_challenge_detector.py.

---

## Decision: mypy --strict Clean (Baseline Retired)

**Date:** 2026-07-26 | **Round:** 18

**What:** The round-11 mypy ratchet baseline (23 findings) was driven to zero.
`strict = true` in pyproject; CI checks core/proxy/orchestrator/api/storage/fetcher/
browser/observability and fails on ANY error. `tools/mypy-baseline.txt` is now empty.

**Why:** Strict was configured but tolerated via the baseline. Closing it caught a
real bug (`api/main.py` called `redis.close()` — the method is `stop()`; shutdown
would have raised AttributeError). Rest were type precision (Protocol for the ASN
classifier, `Any` for duck-typed Playwright pages, one justified
`type: ignore[no-untyped-call]` on the untyped 3rd-party AsyncCamoufox).

**Status:** Active. 64 source files, 0 issues.

---

## Decision: CAPTCHA Solver — NoCaptchaAI Primary, CapSolver Fallback

**Date:** 2026-07-26 | **Round:** 19

**What:** `services/captcha_solver.py::CaptchaSolver` orchestrates two providers:
NoCaptchaAI (primary) with CapSolver (fallback). Both speak the anti-captcha
createTask/getTaskResult protocol; the shared logic lives in
`services/_anticaptcha.py` (`solve_anticaptcha` for token tasks with an arbitrary
task dict, `solve_image_to_text` for OCR, `get_balance`). `build_captcha_solver(budget)`
wires from env keys (NOCAPTCHA_AI_API_KEY, CAPSOLVER_API_KEY). Covers the common
real-world types: reCAPTCHA v2, Cloudflare Turnstile, AWS WAF, GeeTest, MTCaptcha,
image-to-text; hCaptcha via the CapSolver fallback (NoCaptchaAI lacks it — its
method returns None so the orchestrator falls through).

**Why:** NoCaptchaAI is the operator's chosen primary (pay-per-use). Primary→fallback
gives resilience and covers each provider's gaps (hCaptcha) automatically.

**Tradeoffs:** Task-type strings are PROVIDER-SPECIFIC (see troubleshooting — the
public docs are stale). Live-verified accepted forms: NoCaptchaAI reCAPTCHA
`ReCaptchaV2TaskProxyLess`, Turnstile `AntiTurnstileTask`, GeeTest v4 `captchaId`,
MTCaptcha `MTCaptchaTask`, OCR `ImageToTextTask` (`image` field, sync). ImageToText
proven end-to-end (image "HELLO" → "HELLO"). AWS WAF needs per-request runtime
challenge data (no static key) — wired via `**aws_fields`, unverifiable without a
real target.

**Alternatives:** Single provider (rejected — no resilience, coverage gaps).

**Status:** Active and WIRED into the L2/L3 fetch path (round 20 — `fetcher/_captcha.py`
DOM detect→solve→inject→re-poll; worker builds the solver once, factory threads it).
Provider-key health is observable (post-round-20): `captcha_provider_configured`
gauge + `services.captcha_solver.validate_captcha_keys()` + `tools/validate_captcha_keys.py`.
Last preflight: both keys AUTHENTICATE — NoCaptchaAI ~$1 funded, CapSolver $0.00
(top up to enable fallback — external account action, not a code issue).

**Round 22 follow-up — NoCaptchaAI primary live-solve CONFIRMED with real spend.**
`tools/verify_captcha_live.py` targets Google's official reCAPTCHA demo page,
which per this doc's own troubleshooting notes never routes to a solver (demo
sitekeys are unsolvable by design) — that script will hang/fail regardless of
provider health and is the wrong target for a quick confirmation. Used
ImageToText instead (same underlying create-task/poll plumbing as the token
solvers, and known synchronous): a real distorted-text image ("8k4wZ2") sent
to the real NoCaptchaAI API round-tripped an exact match, with real balance
deducted ($0.9984 → $0.9982). Confirms the full pipeline — auth, task
submission, solve, budget deduction — genuinely works end to end on the
funded primary. CapSolver fallback remains unfunded; still needs the account
topped up by whoever holds it, not something fixable from this codebase.

**Round 22 follow-up #2 — reCAPTCHA v2 root-caused with raw API evidence (user
asked to verify independently, not trust the doc above at face value).**
Called `createTask`/`getTaskResult` directly (bypassing this repo's wrapper)
against TWO different real reCAPTCHA v2 sitekeys — Google's own demo page AND
2captcha's demo page (a different, real, non-Google sitekey, ruling out
"Google's specific test key is filtered" as the explanation). Both produced
the identical raw signature: `createTask` returns `errorId: 0` with a real
`taskId` (request genuinely accepted, no rejection), then `getTaskResult`
reports `status: "idle"` on every poll for 45+ seconds straight — never
"processing", never "ready", never an error. The code's own polling loop
(`services/_anticaptcha.py::solve_anticaptcha`) correctly submits, correctly
polls, and correctly gives up after its ceiling — there is no bug in this
repo's CAPTCHA code. `status: "idle"` forever, across two independent real
targets, means the account itself is not routing reCAPTCHA v2 tasks to any
solver — an account-side capability/config gap on NoCaptchaAI's dashboard
(the same "use wallet balance for solving" toggle or per-capability
entitlement troubleshooting.md already named), not a code fix. ImageToText
on the same account/key works with real money in the same session, so the
account and key are fine — this is specifically the reCAPTCHA v2 capability.
Action needed: whoever holds the NoCaptchaAI account needs to check its
dashboard for a disabled/unpurchased reCAPTCHA v2 capability or a
solving-toggle setting, then re-run this same probe to confirm.

**Round 22 follow-up #3 — precise mechanism identified via NoCaptchaAI's own
current docs (user asked to websearch and actually solve it, not just name
the symptom).** `docs.nocaptchaai.com` was recently overhauled ("NoCaptcha
v2... API v2", June 2026) and its error-handling guide documents error code
`2 NO_SLOT_AVAILABLE`: "No worker slot is currently free for your account."
This project's code calls the legacy `POST /getBalance` (clientKey body),
which only returns a bare number. The *new* `GET /balance?apiKey=...`
endpoint returns a richer object — called it directly with the real key:

```
{"balance": 0.9982, "is_default": 1,
 "plan": {"active": 1, "planType": "", "planId": "", "dailyLimit": 0,
          "planLimit": 0, ...}}
```

`planType`/`planId` are empty strings and `is_default: 1` — this account has
**no actual subscription plan**, it's running on wallet-balance-only /
pay-as-you-go mode. Interactive/browser-rendered solving (reCAPTCHA v2,
Turnstile, GeeTest, MTCaptcha) needs a dedicated worker to load and solve the
widget — a "worker slot" — and a no-plan account apparently gets zero
allocated, regardless of wallet balance. ImageToText needs no worker slot
(pure ML inference on a submitted image), which is exactly why it works and
everything else sits at `idle` forever with `errorId: 0` (account genuinely
active, key genuinely valid — just no slot ever frees up).

This is the concrete, actionable fix: **subscribe to an actual plan at
nocaptchaai.com/manage** (not just keep topping up wallet balance) to get
worker-slot capacity for token-based captcha types. Not something fixable
from this codebase — needs the account holder to change the plan, then
re-run `services/nocaptcha.py`'s `solve_recaptcha_v2`/`solve_turnstile`/etc.
(or the raw createTask/getTaskResult probe used here) to confirm slots are
now allocated.

Separately, this project's `get_balance()` should probably be pointed at the
current `GET /balance` endpoint instead of the legacy `POST /getBalance` —
the richer `plan` object is exactly what `tools/validate_captcha_keys.py`
would need to catch this class of problem (no-plan account) automatically
instead of just reporting "balance > 0 = WORKING", which is misleading for
any captcha type that needs a worker slot. Not changed this round (scope was
diagnosis, not a code change) — worth doing as a follow-up.

Turnstile was also attempted against 2captcha's Turnstile demo page, but that
page renders Cloudflare's own published test-only sitekey (`3x0000...FF` —
one of Cloudflare's documented "always challenge" test keys, not a real
production key), which produced a degenerate `{status: "", taskId: ""}`
response — inconclusive, not evidence either way. Needs a real production
Turnstile-protected page to test properly; not done this round (time/token
budget). GeeTest and MTCaptcha: not live-tested this round either. AWS WAF:
still unverifiable without a specific live AWS-WAF-protected target (needs
runtime challenge data extracted from that exact page, no generic demo
exists). hCaptcha: NoCaptchaAI doesn't offer it by design (falls through to
CapSolver), and CapSolver is blocked by its $0 balance — a real, separate,
already-confirmed account issue, not a code gap.


## Decision: Connection Strings — Single Source of Truth Through PgBouncer

**Date:** 2026-07-27 | **Round:** 21 (deploy hardening, PR #3)

**What:** All DB/Redis connection strings come from one place — `StorageConfig`
(`config/schema.py`: `database_url`, `redis_url`), env-overridable via
`${DATABASE_URL}` / `${REDIS_URL}` (reusing `config/loader.py` placeholder
substitution). `api/main.py` and `cli/entrypoint.py` build clients from
`load_config().storage`, not hardcoded strings. Defaults use compose service
names and route the DB through PgBouncer. A field validator strips the SQLAlchemy
`postgresql+asyncpg://` scheme to the plain form asyncpg needs. `PostgresClient`
sets `statement_cache_size=0`.

**Why:** The production deploy exposed three parts of the app connecting three
different ways — `api/main.py` hardcoded a DIRECT `postgres:5432` connection
(bypassing PgBouncer, violating G-05), `cli` hardcoded `pgbouncer:6432`, and the
`rq worker` CLI read `REDIS_URL` from the env (set to `localhost` inside
containers → `Error 111 connecting to localhost:6379`, workers Exited(1)). Three
sources of truth = the root-cause class of "works here, breaks there" bugs.

**Tradeoffs:** `statement_cache_size=0` disables asyncpg's prepared-statement
cache (a small perf cost) but is REQUIRED for correctness through PgBouncer
transaction pooling — the pooler reassigns the backend per transaction, so a
cache keyed to one backend is unsafe. Verified safe: no code path calls
`conn.prepare()`. Alembic is a separate consumer (SQLAlchemy, its own
`alembic.ini` `sqlalchemy.url`) — untouched.

**Alternatives:** Keep `api` on direct-Postgres as a documented asyncpg
workaround (rejected — leaves G-05 violated and the inconsistency in place). The
statement-cache fix makes PgBouncer safe, so honoring the invariant everywhere
was the correct call. Verified: repeated tenant-scoped queries through PgBouncer
succeed (would raise `DuplicatePreparedStatement` without the fix).

**Status:** Active, merged (PR #3, `48b4983`), deployed — api healthy through the
pooler in production.

---

## Decision: SSRF `additional_denied_cidrs` — DI Singleton + Factory, Not 8 Call Sites

**Date:** 2026-07-28 | **Round:** 24 (PR #7)

**What:** `SSRFGuard.__init__` gained one optional param,
`additional_denied_cidrs: list[str] | None = None`, appended to the hardcoded
`DENIED_NETWORKS` list. Rather than threading `config.ssrf_guard.
additional_denied_cidrs` through all 8 zero-arg `SSRFGuard()` construction
sites, only two places actually needed to change: a new `api/dependencies.py::
_ssrf_guard` module-level singleton (populated once in `api/main.py`'s
lifespan, same pattern as `_storage_pg`/`_tenant_resolver`), read directly by
`api/routes.py`'s two route handlers; and `fetcher/factory.py::
_build_ssrf_guard(config)`, called once per fetcher build and passed
explicitly into `Level1/2/3Fetcher`'s existing `ssrf_guard` param.

**Why:** The config field existed (`SSRFGuardConfig.additional_denied_cidrs`)
but had no path to reach any real `SSRFGuard` instance — the constructor took
zero arguments. Two options: change every `SSRFGuard()` call site (8, across
`api/routes.py` ×2, `fetcher/level_1/2/3.py`'s `ssrf_guard or SSRFGuard()`
fallback, and test files), or build the config-aware guard in exactly the two
places production code actually decides whether to build one at all (the API
lifespan, and the fetcher factory that's already the sole DI point for
fetchers per the round-13 factory decision above). The second was strictly
less code and matches an existing pattern instead of inventing a new one.

**Tradeoffs:** Test/live-test call sites (`tests/live/test_smoke.py`,
`tests/integration/test_ssrf_redirect_chain.py`) intentionally keep
zero-arg `SSRFGuard()` — they test default-denied-range behavior, not the
config extension point.

**Alternatives:** Thread the config value through every call site individually
(rejected — 8 sites, most of which already had a working `or SSRFGuard()`
fallback that just needed a config-aware default, not a signature change at
every caller). Read config globally inside `SSRFGuard.__init__` itself via
`load_config()` (rejected — hides a dependency inside a class that otherwise
takes no I/O-touching state, and would silently change behavior for the
existing zero-arg test call sites the moment `additional_denied_cidrs` is
ever set in `config/base.yaml`).

**Status:** Active. Live-verified two ways: a real HTTP POST to the running
API blocked a default-denied range through the DI singleton; a direct
factory-path test confirmed a custom-configured CIDR (`203.0.113.0/24`,
TEST-NET-3) blocks while a normal public IP still passes.

---

## Decision: Real Tracing Backend (Jaeger), Not Just a Configured Exporter

**Date:** 2026-07-28 | **Round:** 24 (PR #7)

**What:** Added a `jaeger` service to `docker-compose.yml` (Jaeger's
`all-in-one` image — native OTLP gRPC receiver on `:4317` plus a UI on
`:16686`, no separate otel-collector needed) and a new
`observability.otlp_endpoint` config field (default `http://jaeger:4317`),
threaded into `configure_tracing()`'s `OTLPSpanExporter(endpoint=...)`.
Auto-instrumented httpx/asyncpg/redis process-wide and added two manual root
spans (job-level, cycle-level — see architecture.md → "Observability &
Tracing"). `configure_tracing()`'s `service_name` param — present before this
round but never used — is now attached via `Resource.create({SERVICE_NAME:
service_name})`.

**Why:** The immediate ask was "wire `configure_tracing()` so it's called" —
doing only that produces a `TracerProvider` with the OTel default exporter
target (`localhost:4317`), which resolves *inside whichever container is
exporting* and therefore never reaches anywhere. A `TracerProvider` that
successfully constructs but never delivers a trace anywhere observable is
tracing in name only — the user explicitly asked for it to be "fully
functional and deployed," which means a real backend has to exist and
receive real data, provable by querying it, not just by the absence of an
exception.

**Tradeoffs:** Jaeger's all-in-one image is dev/single-node — fine for this
project's current docker-compose deployment model, would need a real
collector + persistent backend (Tempo, a hosted Jaeger, etc.) for a
multi-node production deployment. Not attempted — no such deployment target
exists in this repo yet (same reasoning as the GHCR build-and-push job:
publish/wire what exists, don't invent infrastructure that isn't there).

**Alternatives:** Ship only the `TracerProvider` + exporter and call it done
once it didn't crash (rejected — this is exactly the "looks wired, does
nothing" class of bug the whole round-24 audit exists to close; verified via
Jaeger's own query API that traces were *actually* landing, not inferred from
log silence — log silence turned out to be ambiguous evidence during this
same investigation, see troubleshooting.md). A generic OTel Collector in
front of Jaeger (rejected — extra moving part with no present benefit; add it
if/when a second trace backend or sampling/processing pipeline is needed).

**Status:** Active. Live-proven end to end, including through a real forked
rq work-horse (see the next decision) — a real `/v1/scrape` job produced a
`scrape_job` trace in Jaeger with 32 nested Postgres/Redis child spans;
`proxy_daemon_harvest/promotion/health/retention` spans confirmed separately.

---

## Decision: `force_flush()` in the rq Job's `finally` Block, Not `atexit`

**Date:** 2026-07-28 | **Round:** 24 (PR #7)

**What:** `orchestrator/tasks.py::_run_scrape_job`'s `finally` block calls
`trace.get_tracer_provider().force_flush(timeout_millis=2000)` (guarded by
`hasattr` — the default no-op provider has no `force_flush` at all),
alongside the existing `s3.stop()`/`redis.stop()`/`pg.stop()` cleanup. The
`OTLPSpanExporter` itself is also constructed with an explicit `timeout=2`
(seconds) — `force_flush`'s own timeout only bounds how long `force_flush`
waits, not the underlying exporter's own default (10s) network-call deadline.

**Why:** Confirmed live that a real rq worker processing a real job never
produced a trace, while calling the exact same `_run_scrape_job` function
directly (not via `rq worker`'s job-dispatch path) worked immediately — same
code, same process type, different result. Root cause: rq's `Worker.
perform_job()` runs inside a forked "work horse" child process
(`rq/worker/base.py`'s own docstring: "Will/should only be called inside the
work horse's process") that terminates via `os._exit()` — confirmed by
grepping rq's actual installed source (`rq/worker/base.py:1619`: "os._exit()
is the way to exit from childs after a fork()"). `os._exit()` skips `atexit`
entirely (already registered by `configure_tracing()` for the *non*-forking
processes — api, cli, harvester daemon) — and separately,
`BatchSpanProcessor`'s background export thread doesn't survive `fork()` at
all regardless (only the calling thread continues in a forked child), so the
already-queued span from `configure_tracing()`'s original exporter setup
would never be flushed by anything unless something explicit forces it
before the process disappears.

**Tradeoffs:** `force_flush()` is a best-effort synchronous export — if
Jaeger is genuinely unreachable, every job now pays up to ~2s of latency
before the ceiling gives up (previously: 0ms, because nothing flushed at
all). Deliberately bounded short rather than trusting the SDK's default
30s — an unreachable trace collector must never be able to stall the actual
scrape/crawl pipeline. First attempt used no explicit timeout at all, which
measurably slowed the local test suite (50s → 78s) — the two-part fix
(`force_flush`'s own bound AND the exporter's own bound) was needed together;
either alone left the other's default in control of worst-case latency.

**Alternatives:** `atexit.register(provider.shutdown)` alone (tried first,
insufficient — confirmed via live testing that `os._exit()` bypasses it;
kept anyway for the non-forking processes where it's the correct mechanism).
Switch `orchestrator/tasks.py`'s span export to `SimpleSpanProcessor`
(synchronous export on every span end, no batching) instead of
`BatchSpanProcessor` (rejected — `configure_tracing()` is shared by every
process, including the API and harvester daemon where batching is the
correct choice for a busy, long-running process; changing it globally to
fix a fork-specific problem in one caller would trade one process's problem
for another's).

**Status:** Active. Regression test added:
`tests/unit/test_tasks.py::test_run_scrape_job_creates_traced_span_with_job_attributes`
uses `InMemorySpanExporter` (added to the real, already-configured
`TracerProvider` via `add_span_processor` — `trace.set_tracer_provider()`
can't be called twice) to assert the span exists with the right attributes,
not just that nothing crashes. Live-proven separately through an actual
forked rq work-horse, not just this in-process test.

---

## Decision: Redis-Backed Scrape-Time Metrics, Not In-Process Gauges

**Date:** 2026-07-28 | **Round:** 25

**What:** Every round-25 metric whose triggering event happens outside the
`api` process (`dlq_size`, `capsolver_daily_spend`/`_ceiling`,
`circuit_breaker_trips_total`, `proxy_exhausted_total`,
`job_duration_seconds_count`/`_sum`, `proxy_source_healthy`) writes a plain
value to Redis or queries Postgres directly at event time, then
`observability/metrics.py`'s `refresh_*` functions read that back into a
local `Gauge` only when `/metrics` is actually scraped by the `api` process.
Full mechanics: `.claude/knowledge/architecture.md` → "Metrics: Cross-Process
Emission Pattern".

**Why:** `prometheus_client`'s `REGISTRY` is in-process global state. These
6 metrics' triggering events happen in rq worker processes or the
`proxy-harvester` daemon — different processes from the one serving
`/metrics`. Worse for rq specifically: it forks a fresh "work horse" process
per job that `os._exit()`s immediately after, so even a well-intentioned
in-process `Counter.inc()` there is gone before the next scrape could ever
see it. Setting an in-process Gauge from worker code would have looked
wired (code compiles, tests could even pass if they don't check
cross-process visibility) while being exactly as functionally dead as the
config-wiring gaps this whole round exists to close.

**Tradeoffs:** More Redis round-trips at scrape time (one extra `GET` per
metric per `/metrics` hit) versus zero for a pure in-process gauge — judged
acceptable since `/metrics` is scraped on a slow interval (typically 15-60s
in Prometheus), not per-request. `job_duration_seconds` and `dlq_size` are
Gauges reconstructed from a plain Redis counter/Postgres count, not native
`Histogram`/`Counter` objects — this means no real histogram buckets for
`job_duration_seconds` (just count + sum per status label), which is enough
to satisfy the existing `HighJobFailureRate` alert's query but would need
real bucketing added if a latency-distribution view is ever needed.

**Alternatives considered:**
- **Prometheus Pushgateway** — the standard solution for exactly this
  class of problem (short-lived batch jobs pushing metrics). Rejected for
  this round as new infrastructure beyond scope; worth reconsidering if the
  Redis-round-trip-per-scrape approach doesn't scale.
- **A dedicated metrics HTTP server per rq worker process** — rejected
  outright: rq's per-job forking means a worker process typically lives for
  the duration of one job, far shorter than a Prometheus scrape interval;
  the server would usually be dead again before anything could reach it.
- **True native `Histogram` for `job_duration_seconds`** — would require
  the observe() call to happen in the same process serving `/metrics`,
  which isn't possible given where jobs actually run. Rejected in favor of
  the simpler count+sum-as-Gauges reconstruction, which is enough for the
  one alert that currently needs it.

**Status:** Active. Live-verified: rebuilt the `api` container, curled
`/metrics` twice, confirmed real values (403 real DLQ rows, 3 real tenants'
CapSolver spend/ceiling, `http_requests_total` incrementing across scrapes).
`promtool check rules` (from the actual running Prometheus container)
validated the edited `monitoring/alerts/prometheus_rules.yml` syntax.

---

## Decision: BrowserPool Mismatch Handling — Keep as Spare, Domain Relaxed, Proxy Not

**Date:** 2026-07-28 | **Round:** 25 (user follow-up after initial round-25 pass)

**What:** `browser/pool.py::acquire()` no longer tears down (`__aexit__`s) a
pooled wrapper just because it doesn't match the current request's domain or
proxy — it's kept in the pool as a live spare for a future request that does
match, and a fresh wrapper is built for the current one instead (bounded by
the same `core.budget.BROWSER_SEMAPHORE` either way). Separately, a wrapper
whose `_last_domain` is still `None` (never successfully leased — this is
every prewarmed instance) is now treated as an domain match for any request,
not a mismatch. **Proxy mismatch does NOT get that same "unclaimed matches
anything" treatment.**

**Why:** The class's own docstring already said tear-down should only
happen "on unhealthy release, idle timeout, or explicit shutdown" — the
prior mismatch-destroys behavior contradicted its own documented contract,
and (per the original design spec §3.5) the pool is meant to be "purely a
latency optimization... not a concurrency control," which destroying good
instances on every rotation defeats. The domain relaxation specifically
closes a real regression: prewarmed browsers were being evicted on their
very first real acquire() call (their `_last_domain` starts `None`, and
`None != "example.com"` was being read as a mismatch), making prewarming
close to useless. Domain relaxation is safe because a browser with no
domain history has no functional reason it can't serve any domain.

Proxy is different, and deliberately not relaxed the same way: a prewarmed
`CamoufoxWrapper` is launched with `proxy=None` baked into the Camoufox
constructor call at process-start — Playwright/Camoufox has no way to
change a running browser's proxy after launch. If `wrapper.proxy is None`
were treated as "matches any requested proxy," a proxy-scoped request could
silently be served through a proxy-less browser — quietly dropping the
proxy entirely, not just picking the wrong one. That's a real functional
bug (defeats IP rotation/anti-detection for that fetch), not a cosmetic
labeling issue like the domain case.

**Tradeoffs:** A prewarmed browser genuinely cannot help the *first* fetch
of any new (domain, proxy) combination it wasn't already scoped to — that
fetch always pays a fresh Camoufox launch, no way around it without knowing
the future proxy in advance (which isn't possible; proxies are assigned per
request). What the domain relaxation actually buys is avoiding *destroying*
the prewarmed instance for having failed one mismatched check — it stays
available for a subsequent proxy-less request, or after `BrowserPool`'s
`_last_domain` machinery would otherwise be forced to keep rebuilding.

**Alternatives considered:**
- **Prewarm each instance with a real proxy** — rejected, not possible in
  general; proxies aren't known until a request arrives.
- **Relax proxy the same way as domain** — rejected outright per the
  functional-bug reasoning above; this was seriously considered and
  discarded, not overlooked.
- **"Upgrade" a mismatched wrapper's proxy in place** — rejected; no
  Camoufox/Playwright API exists to reconfigure a running browser's proxy
  after launch.

**Status:** Active. Regression tests:
`tests/unit/test_browser.py::TestBrowserPool::
test_prewarmed_wrapper_not_evicted_on_first_domain_mismatch` and
`::test_mismatched_wrapper_kept_in_pool_not_destroyed`. Live-verified via
`tests/live/test_browser_pool_lifecycle.py` (real Camoufox processes) after
the change.

---

## Decision: Botasaurus — Deleted as Dead Code, Then Restored For Real (Same Round)

**Date:** 2026-07-28 | **Round:** 25

**What:** `fetcher/botasaurus_wrapper.py` was deleted early in round 25 (as
part of closing the round-24 "orphaned module" finding), then restored and
wired for real later in the same round, per an explicit user follow-up ask
("implement Botasaurus for real per spec"). Final state: `botasaurus==4.0.97`
is a genuine dependency; `Level2Fetcher` tries a real `BotasaurusWrapper`
fetch first, falling back to the existing Camoufox pipeline on failure or a
detected challenge page. Full design: `.claude/knowledge/architecture.md` →
"Botasaurus Integration".

**Why deleted first:** `fetcher/botasaurus_wrapper.py` was never imported by
anything in production, `botasaurus` wasn't a declared dependency anywhere
(not `pyproject.toml`, Dockerfile, or CI), and `config/schema.py`'s
`level_2.engine: "botasaurus+camoufox"` value was fiction nothing read — L2
had always run as Camoufox-only in practice. Per the "remove broken code,
don't leave it orphaned" operating rule, and because a Literal-typed
`engine` field honestly reflecting reality (`"scrapling" | "camoufox"`)
seemed better than a config value describing a feature that had never
existed in production.

**Why restored:** the authoritative spec (`.local/specs/scraper-engine-blueprint-v2.md`,
local-only, not tracked in git — §3.6) explicitly designs a real `BotasaurusWrapper` with a specific
concurrency-coordination fix (F-32: force `parallel=1` since Botasaurus
manages its own multiprocessing pool internally) — this was a deliberate,
documented piece of the original architecture, not an accidental leftover.
Deleting it without asking first was too large an architectural call to
make unilaterally; user confirmed after being told the spec designed a real
implementation.

**What was found restoring it:** the *original* deleted file's
`_botasaurus_fetch` called `driver.page_source` — an attribute that doesn't
exist on botasaurus's real `Driver` class (confirmed against the installed
package; it exposes `driver.page_html` instead). Even if the original file
had been wired into the fetch path, it would have raised `AttributeError`
on its first real fetch — it was never actually tested against a real
install at any point in the project's history. The restored version fixes
that, but is otherwise deliberately minimal: `parallel=1`, headless/xvfb,
`proxy=`, `profile=`, plain `get()`→`page_html`, matching what the spec's
own §3.6 code sample shows. `google_get`/`bypass_cloudflare` and the rest of
Botasaurus's anti-detection surface were deliberately **not** added in this
pass — that's the separate, already-researched-but-not-yet-implemented
follow-up tracked in `.claude/MEMORY.md` → Technical Debt (round 25
follow-up).

**Tradeoffs:** `Level2Fetcher` now makes up to two fetch attempts
(Botasaurus, then Camoufox) before escalating to L3 on failure — more
latency on the failure path, in exchange for a real chance of a cheaper/
different-fingerprint success on the happy path. Botasaurus fetches are
one-shot (`reuse_driver=False`), not pooled like `CamoufoxWrapper` —
consistent with the minimal-restoration scope above.

**Alternatives considered:**
- **Leave it deleted, correct the config value (the original round-25
  decision)** — reasonable and defensible on its own; reversed only because
  the user, once told the spec designed a real implementation, wanted it
  built rather than the config just being made honest about its absence.
- **Wire Botasaurus as the ONLY L2 engine (replacing Camoufox for L2
  entirely)** — rejected; Botasaurus's Selenium-style driver can't run the
  existing challenge-detection/captcha-solve/scroll pipeline, so an
  unconditional replacement would have made L2 strictly less capable on
  anything past a simple connection-level check. Fallback-not-replacement
  keeps L2 at least as capable as before, with a chance of doing better/cheaper.

**Status:** Active. `tests/unit/test_botasaurus_wrapper.py` (7 tests) covers
the semaphore acquire/release, the forced `parallel=1`, and all 4
fallback/pass-through branches of `Level2Fetcher.fetch()`. Live-verified:
real headless-via-Xvfb Chrome launched and fetched a `data:` URL in the dev
sandbox before the final implementation was written; every container
(`api`, `worker-l1/l2/l3`, `proxy-harvester`) rebuilt and confirmed
`import botasaurus` succeeds inside the actual image, not just the local venv.

## Decision: src/ Layout Over Flat Top-Level Packages

**Date:** 2026-07-29 | **Round:** 27

**What:** Consolidated 12 previously-separate top-level Python packages
(`api/`, `browser/`, `cli/`, `config/`, `core/`, `fetcher/`,
`observability/`, `orchestrator/`, `proxy/`, `services/`, `storage/`,
`scrapy_project/`) under a single `src/scraper_engine/` package. Imports
changed from `from core.tenant import TenantId` to `from
scraper_engine.core.tenant import TenantId`. `tests/` and `migrations/`
stay at repo root, unmoved.

**Why:** Flagged unprompted while reviewing the root layout like a senior
developer — 12 sibling top-level packages with no umbrella package is a
flatter, older-style layout; the modern convention for anything meant to be
installed as one library is a single `src/<package>/`. User asked for a
formal plan, then approved it.

**Alternatives considered:**
- **Leave it flat.** Rejected — not wrong, but not what "professional repo"
  means to the user, and was explicitly the item flagged for this change.
- **Only partially consolidate** (e.g. wrap just the packages with the most
  cross-references). Rejected — a partial umbrella package is a worse
  mental model than either extreme; either everything is `scraper_engine.X`
  or nothing is.

**Tradeoffs:**
- Real, one-time cost: 455 import statements needed rewriting, and the
  rewrite surfaced import forms invisible to static regex tooling (bare
  dotted imports with rebinding semantics, quoted-string module references
  in `mock.patch`/`monkeypatch.setattr`/rq's job queue/Scrapy's own config)
  — see `.claude/knowledge/troubleshooting.md` → the three round-27 entries
  for the exact gotchas hit.
- The Dockerfile needed a real fix (`pip install --no-deps .` after
  `COPY .`, not a `PYTHONPATH` patch) since the container previously relied
  on the flat packages landing directly in `WORKDIR /app`.
- `pyproject.toml`'s packaging/coverage/isort config, and every hardcoded
  package-path argument in CI/`CONTRIBUTING.md`/`README.md` (`mypy core/
  proxy/ ...` style commands) needed updating in lockstep — this is the
  exact same "N places that don't read from each other" class of drift as
  Known Operational Gaps #12, just for paths instead of dependency
  versions.

**Verification standard used:** static analysis (ruff/mypy) was not
sufficient by itself — the real proof was submitting a live job through the
rebuilt container stack and confirming `PENDING → COMPLETED` with real
fetched content, since the single highest-risk fix (rq's dotted-string job
reference) is invisible to any import-checker.

## Decision: `.archive/` and `.local/` as Two Separate Gitignored Directories

**Date:** 2026-07-29 | **Round:** 27

**What:** Historical per-round evidence/directive/closure reports live in
`.archive/{evidence,directive,closure,other}/`. The design spec, a
confirmed-duplicate directory, and unused manual scripts live in a
**separate** `.local/` directory. Both are gitignored — kept on disk,
absent from GitHub.

**Why two directories instead of one:** First pass used a single
`docs/archive/` (tracked in git). User rejected this twice: first for still
being tracked at all ("I don't see this kind of file in other developers'
GitHub repos"), then — after `.archive/` was made gitignored and
categorized by report type — for having non-report files (`specs/`, a
duplicate directory, scripts) dumped into the same categorized bucket
("these files are not docs"). The user's own correction was specific: a
folder whose subdirectories are named after document categories
(evidence/directive/closure) is the wrong home for a Python script or a
whole spec file, even if both end up gitignored for the same reason.

**Alternatives considered:**
- One folder, mixed content, `other/` catch-all for everything non-doc
  (the first attempt at reconciling). Rejected by the user directly.
- Delete the non-doc items instead of relocating them. Not chosen — nothing
  in this repo gets deleted outright when it can instead be archived/moved;
  git history plus a local, findable copy is preferred over relying on
  `git log` archaeology to recover something later.

**Status:** Active. `.gitignore` has two separate entries (`.archive/`,
`.local/`); do not merge them back into one directory without the same
user correction applying in reverse.

## Decision: `types-redis` Removed, Not Version-Pinned Differently

**Date:** 2026-07-29 | **Round:** 27

**What:** Removed `types-redis>=4.6.0` from `pyproject.toml`'s dev
optional-dependencies entirely, rather than trying to pin it to a version
compatible with the installed `redis==8.0.1`.

**Why:** Real `redis` (since some version well before 8.0.1) ships its own
inline types (`py.typed` marker present in the installed package). A
third-party stub package for a library that now types itself is redundant
at best; at worst — confirmed here — it's actively wrong when it targets
an old version of that library and gets resolved instead of the real types.
CI never installed `types-redis` in the first place and was checking
against the correct types the whole time; removing the stub makes local
match CI instead of the reverse.

**Alternatives considered:**
- Pin `types-redis` to a version matching installed `redis`. Rejected —
  checked, no such version exists; `types-redis` is a legacy, unmaintained
  stub for redis-py versions that predate its own `py.typed` types, not an
  actively-updated companion package.
- Add `# type: ignore` comments to satisfy whichever mypy result showed up
  locally. Rejected as the first attempt, then reverted — this would have
  masked real, correctly-typed methods (`aclose()`, `eval()`) behind
  unnecessary ignores, treating a stub gap as if it were a real gap in
  redis-py's own types.

**Status:** Active. Do not re-add `types-redis` to dev dependencies.
