# Design Decisions

**Purpose:** Record WHY decisions were made, TRADEOFFS considered, ALTERNATIVES rejected.
**Scope:** Irreversible or high-cost decisions. Routine implementation choices excluded.
**When to read:** Before changing architecture; when a decision seems wrong and needs context.
**Related:** `specs/scraper-engine-blueprint-v2.md`, `.claude/knowledge/architecture.md`

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
