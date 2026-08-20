# Design Decisions

**Purpose:** Record WHY decisions were made, TRADEOFFS considered, ALTERNATIVES rejected.
**Scope:** Irreversible or high-cost decisions. Routine implementation choices excluded.
**When to read:** Before changing architecture; when a decision seems wrong and needs context.
**Keywords:** design rationale, tradeoffs, alternatives considered, ADR,
rejected approaches, why-not-X.
**Dependencies:** none — each entry is self-contained; cross-references
`architecture.md` for the resulting design and `technical-debt.md` for the
round it shipped in.
**Related:** `.local/specs/scraper-engine-blueprint-v2.md` (local-only, not tracked in git), `.claude/knowledge/architecture.md`, `.claude/knowledge/technical-debt.md`

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

## Decision: Multi-Endpoint Public Judge, Superseding the Self-Hosted Judge

**Date:** 2026-08-07 | **Round:** 32

**What:** `proxy/harvester.py::_http_validate()` (and `health_monitor.py::
check_one()`, which now imports the same list) validate proxies against
`JUDGE_URLS` — an ordered tuple of independent, differently-hosted public
IP-echo services (`httpbingo.org`, `api.ipify.org`, `postman-echo.com`,
all plain HTTP), trying each in turn and stopping at the first 200. This
supersedes round 6's self-hosted judge (`proxy/judge_server.py`, a
loopback `http.server` on port 8089, built specifically to remove a
`httpbin.org` dependency).

**Why:** Live re-verification of the escalation ladder found the proxy
pool permanently stuck at score 25 (never promoted) despite the judge
server running correctly. Root cause: when a request routes through a
forward proxy, the *proxy* resolves the target address — a loopback
address (`127.0.0.1`) always means "the proxy's own machine" to it, never
the machine that sent the request. Confirmed live: real proxies returned
their own internal service responses (`MiCGI-Upstream: 127.0.0.1:8089`,
various 502/503s) instead of ever reaching our judge. This is
architectural, not a config or startup bug — a loopback judge can never
validate a real third-party proxy, correctly running or not, and has been
broken this way since round 6 without being caught, because the one
integration test covering this path seeds a "proxy" pointing at the
judge's own address (a degenerate case real external routing never
produces).

The first fix attempt — pointing at a single public target (`httpbin.org`,
matching `health_monitor.py`'s prior choice) — was found live-down
(persistent 503s across every scheme and repeated attempts, not
transient) while building and testing this exact change. That is direct,
observed evidence that a single public service must never be a hard
dependency for proxy scoring, not just a theoretical risk.

**Tradeoffs:** Depends on three external services instead of zero (fully
offline) or one. Mitigated by requiring only ONE of the three to be up at
any given time, chosen from different hosting providers/orgs to reduce
correlated-outage risk. Plain HTTP only (not HTTPS) — an HTTPS target
routed through an HTTP forward proxy needs CONNECT tunneling, which many
free HTTP-only proxies can't do; all three candidates confirmed to serve
plain HTTP directly (no forced redirect). Worst-case validation latency
per proxy grows from one timeout to up to `timeout * len(JUDGE_URLS)` if
every candidate is simultaneously unreachable — accepted since this only
runs from already-bounded-concurrency contexts (harvest is sequential,
`ProxyPromotionJob` caps concurrent validations at 5), never a
request-path hot loop.

**Alternatives considered:**
- **Fix the self-hosted judge by exposing it publicly instead of on
  loopback** — rejected. It's stdlib `ThreadingHTTPServer` with no
  built-in timeout, request-size limit, or slow-read (slowloris)
  protection, and one thread per connection with no cap — not hardened
  for the open internet, and its own docstring already said
  "Internal-only — never expose publicly" for good reason. Would trade
  one bug for a worse one (unbounded thread growth under a trivial
  slow-connection attack) rather than fixing the actual problem.
- **Single public target (`httpbin.org`)** — tried first, found live-down
  while implementing it; directly disproven as robust by observed
  evidence, not just a theoretical concern.
- **Add a concurrency cap on validation calls** — considered, then
  checked against the real code and dropped: every real call site is
  already bounded (`_scrape_one`'s harvest loop is fully sequential;
  `ProxyPromotionJob` already wraps its only concurrent fan-out in
  `asyncio.Semaphore(PROMOTION_CONCURRENCY=5)`). No new rate-limiting
  code was needed.

**Status:** Active. `proxy/judge_server.py` remains in the tree as a
deterministic, network-independent stand-in for tests only
(`tests/unit/test_judge_server.py`, `tests/integration/test_promotion.py`)
— no longer wired into `proxy/harvester_daemon.py`'s production startup.

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
follow-up tracked in `.claude/knowledge/technical-debt.md` (round 25
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

---

## Decision: Coverage Gate — One Combined Run in `chaos`, Not Per-Job

**Date:** 2026-07-29 | **Round:** 28

**What:** `--cov=src/scraper_engine --cov-fail-under=100` runs once, in the
`chaos` job's final pytest invocation (`tests/unit/ tests/integration/
tests/chaos/` together). The `unit` and `integration` jobs run their own
subset without `--cov` at all.

**Why:** Coverage data (`.coverage`) doesn't cross GitHub Actions job
boundaries — each job is a fresh runner/filesystem. Measuring per-job and
combining would need `coverage combine` plus `actions/upload-artifact` /
`download-artifact` to carry `.coverage` files between `unit` →
`integration` → `chaos`, for a gate that only makes sense evaluated against
the *whole* suite (a file fully covered by an integration test shouldn't
fail the unit job's own number). `chaos` already `needs: integration`
(which `needs: unit`), already brings up full docker-compose infra
(Postgres/Redis/PgBouncer/MinIO), and is the only job that ever runs all
three test directories together — so it's the one place a single
`--cov-fail-under=100` invocation can honestly represent "the whole test
suite covers 100% of the code," which is what the gate is actually meant to
assert.

**Alternatives considered:**
- Per-job `--cov` + artifact-based `coverage combine` across jobs. Rejected
  — real complexity (upload/download steps, combine step, a 4th place
  `--cov-fail-under` could live) for a gate that's simpler to reason about
  as "the last job that has everything running enforces the final number."
- `--cov` on every job, each gated independently at a lower threshold.
  Rejected — three different partial-coverage thresholds to maintain in
  sync is its own drift risk (the exact bug class this whole round is
  about), and `unit`-only coverage of integration-tested code would be
  arbitrarily low regardless of tuning.

**Status:** Active. If `unit`/`integration`'s now-`--cov`-free runs are
ever reconsidered (see `.wolf/STATUS.md` → Next phase), keep the actual
gate — `--cov-fail-under=100` — in exactly one place; don't let it drift
back into two or three.

**Follow-up resolved, round 62:** the deferred question — whether
`unit`/`integration`'s bare (no-`--cov`) pytest runs are worth keeping —
is closed. **Keep both jobs.** The `needs:` chain
(`lint → unit → integration → chaos → build-and-push`) is strictly
sequential, not parallel, and each stage's *infra* cost strictly
increases: `unit` has zero `services:`/docker setup, `integration` adds
GH Actions `services:` containers for Postgres+Redis, `chaos` brings up
the project's full docker-compose stack (Postgres/Redis/PgBouncer/MinIO,
4 containers including real PgBouncer transaction-pooling `services:`
can't replicate) plus coverage instrumentation on top. "Redundant" in the
original framing meant only that `unit`'s and `integration`'s test *files*
get executed a second time inside `chaos`'s combined run — not that the
jobs themselves add nothing. Removing them would mean every ordinary test
failure only surfaces after the heaviest job's full infra spinup, with no
compensating CI-time savings on the happy path (`chaos` still has to run
every test file regardless, per this same decision's "one combined run"
design above). Verified via `.github/workflows/test.yml`: no parallelism
exists to reclaim by merging them.

---

## Decision: Scrapling Engine — Manual Redirect Loop, Not `follow_redirects=True`

**Date:** 2026-07-29 | **Round:** 28

**What:** `ScraplingWrapper.fetch()` always calls Scrapling's
`AsyncFetcher.get(..., follow_redirects=False)` and returns a
`ScraplingResponse(status_code, text, location)`. `Level1Fetcher.
_fetch_via_scrapling` drives its own loop over that response, resolving
`location`, calling `self._ssrf_guard.validate(next_url)`, then fetching
the next hop itself — the same shape `_fetch_via_ja3` already used for the
JA3-matched client (round 26) and the plain httpx path uses natively.

**Why:** Spec §1.1 #4 (non-negotiable) requires every redirect hop
re-validated against the SSRF guard, not just the initial URL — closing
the DNS-rebind/redirect-to-internal-target TOCTOU gap (round 22). Letting
Scrapling follow redirects itself (`follow_redirects=True` or its default
`"safe"`) would hand that hop-by-hop decision to a library with no
awareness of this guard at all — a redirect straight to a private/metadata
address would go through unchecked, silently reopening the exact gap round
22 closed for the other two engines.

**Alternatives considered:**
- Trust `follow_redirects="safe"` (Scrapling's default) and only validate
  the final landed URL. Rejected outright — this is precisely the
  submit-time-only check the round-22 fix replaced; a multi-hop redirect
  chain could touch a denied address mid-chain without ever surfacing in
  the final URL.
- Give `ScraplingWrapper` its own `SSRFGuard` and validate internally.
  Rejected — would duplicate the guard instance Level1Fetcher already
  owns (`self._ssrf_guard`, config-built via `fetcher/factory.py::
  _build_ssrf_guard`), risking the two guards drifting out of sync
  (different `additional_denied_cidrs`) the same way the pre-round-24
  8-call-site `SSRFGuard()` construction did (see the SSRF DI decision
  above).

**Status:** Active. Any future engine added to `Level1Fetcher` (a 4th
first-attempt path) should follow this same shape — return a raw,
redirect-unresolved response and let `Level1Fetcher` own the loop — rather
than trusting the underlying library's own redirect handling.

---

## Decision: Scrape Cache Reuses `scrape_results`, No New Cache Table

**Date:** 2026-07-29 | **Round:** 29

**What:** `Worker._check_cache` queries the existing per-tenant
`scrape_results` table directly (`WHERE url = $1 AND success = true AND
extracted_at > NOW() - INTERVAL '7 days' ORDER BY extracted_at DESC LIMIT
1`), reusing the table's existing `(url, content_hash)` index rather than
introducing a dedicated cache table/Redis structure. `CACHE_TTL_DAYS = 7`
is a **sliding** window: a cache hit persists its own fresh
`scrape_results` row (via the same `on_result`/`_persist_one_result` path
as a real fetch), which extends freshness from that moment rather than
counting down from the original scrape.

**Why:** `scrape_results` already has everything a cache hit needs to
reconstruct a `FetchResult` — `markdown`, `json_data` (→ `extracted`),
`html_snapshot_url`, `level_used`, `http_status` — and already has an
index with `url` as the leading column, so a lookup by URL alone uses it
efficiently. A separate cache store would duplicate data that's already
being written on every successful scrape for an unrelated reason (the
normal results/audit trail), and would need its own invalidation logic
kept in sync with `scrape_results`' own lifecycle. Sliding TTL (vs. a
fixed clock from the original scrape) was chosen because it needs zero
extra bookkeeping — the existing `extracted_at DESC LIMIT 1` query is
naturally "most recent", whether that's the original scrape or the last
reuse — and matches how most caches (e.g. HTTP `Cache-Control` refreshed
by revalidation) behave in practice; a strict "exactly 7 days from first
scrape, never longer" semantic would need tracking a separate
"originally_scraped_at" column and was explicitly flagged to the user as
the alternative, not silently assumed.

**Alternatives considered:**
- New dedicated `scrape_cache` table (or Redis hash) keyed by URL.
  Rejected — pure duplication of `scrape_results`' existing content for no
  new capability; two places to keep in sync, two invalidation policies.
- Fixed TTL anchored to first-scrape time. Rejected for the reason above —
  real but small extra bookkeeping for a stricter guarantee nobody asked
  for; can be added later without touching the lookup query's shape if a
  real need for it shows up.
- Cache scoped globally (cross-tenant) rather than per-tenant. Rejected —
  every other query in this codebase is tenant-scoped for isolation
  (`self._pg.fetchrow(tenant_id, ...)`); a cross-tenant cache would leak
  "did another tenant scrape this URL" and let one tenant's extraction
  settings silently serve another's request.

**Status:** Active. Directly coupled to the S3 retention decision below —
`CACHE_TTL_DAYS` and `S3Client.SUCCESS_RETENTION_DAYS` must stay equal (or
the S3 retention must stay ≥ the cache TTL); both constants document this
cross-dependency in their own comments.

---

## Decision: S3 Success-Snapshot Retention Bumped 1 Day → 7 to Match Cache TTL

**Date:** 2026-07-29 | **Round:** 29

**What:** `S3Client.SUCCESS_RETENTION_DAYS` changed from `1` to `7`.

**Why:** Round 22's original BD-07 policy set successful snapshots to
expire after 1 day on the reasoning that "content is already extracted" by
then, so the raw HTML has no further use. Round 29 changed two things that
invalidate that reasoning: (1) `html_snapshot_url` is now actually
returned to callers (previously computed and silently dropped — see the
technical-debt.md round-29 entry, item 1), so a caller might reasonably
expect to fetch it; (2) the new cache-reuse feature tells callers a
result is "fresh" for 7 days, but if the backing S3 object was already
gone after 1, the pointer would be a dead link for 6 of those 7 days. Not
bumping this would have shipped a caching feature that quietly returns
broken links most of the time.

**Alternatives considered:**
- Leave retention at 1 day, strip `html_snapshot_url` back out of cache-hit
  responses specifically. Rejected — inconsistent (a fresh scrape's
  pointer works, a cache hit's doesn't) and reintroduces exactly the gap
  item 1 just closed, just conditionally.
- Decouple the two constants entirely (cache says "fresh" for longer than
  the snapshot is guaranteed to exist). Rejected — a caller has no way to
  know the pointer might already be dead without hitting S3 and finding
  out; better to make the guarantee actually hold.

**Status:** Active. Failed snapshots still retain 30 days (unchanged,
BD-07) — that number was never about "how long is this useful," it was
about debugging window for failures, which this round didn't touch.

---

## Decision: Idempotency Dedup — Non-Unique Index + Query-Time Exclusion, Not a DB Constraint

**Date:** 2026-07-29 | **Round:** 29

**What:** Migration 005 adds `scrape_jobs.idempotency_key TEXT` with a
plain **non-unique** btree index. `POST /v1/scrape`/`/v1/crawl` look up
`WHERE idempotency_key = $1 AND status NOT IN ('FAILED', 'CANCELLED',
'DEAD_LETTER') ORDER BY created_at DESC LIMIT 1` *before* the quota charge,
returning the existing job if found instead of creating a new one.

**Why:** A hard unique constraint on `idempotency_key` would make the
*second* legitimate use of the same key — a genuine retry after the first
attempt died (`FAILED`/`CANCELLED`) — a database error instead of a
sensible new job. "Idempotent" here means "don't double-submit *live*
work," not "this key may only ever be attached to one job for all time."
Excluding dead terminal states from the lookup encodes that meaning
directly in the query rather than requiring a workaround (e.g. deleting
the old row, or generating a synthetic suffixed key) to satisfy a
constraint that's stricter than the actual intent.

**Alternatives considered:**
- Unique constraint on `idempotency_key`, with the API layer catching the
  resulting DB error and translating it. Rejected — pushes business logic
  (what counts as "the same request") into exception-handling around a
  constraint violation instead of an explicit, readable query condition;
  also would need a partial unique index (`WHERE status NOT IN (...)`) to
  even express the same intent, which is more DB-specific machinery for
  the same result.
- Store the idempotency key in Redis (TTL'd) instead of Postgres. Rejected
  — `scrape_jobs` already has `created_at`/`status` needed for the dead-
  state exclusion and the "most recent" ordering; a Redis-side store would
  need to duplicate or query back to Postgres for status anyway, and loses
  the automatic consistency of living in the same transactional row.

**Status:** Active. The dedup check runs before the quota charge
specifically — that ordering is the actual bug fix (a retry must not
double-charge); reordering it after quota in any future change would
silently reintroduce the double-charge this round closed.

---

## Decision: Markdown Conversion Centralized in `Worker.process_job`, Not Per-Fetcher

**Date:** 2026-07-29 | **Round:** 29

**What:** All three inline `self._firecrawl.convert_to_markdown(...)` call
sites inside `fetcher/level_1.py` were deleted, along with
`Level1Fetcher`'s `firecrawl_client` constructor parameter. Markdown
conversion now happens once, inside `Worker.process_job`, immediately
after `AdaptiveSelector` extraction — the same location, right after
whichever escalation level actually succeeded.

**Why:** Firecrawl conversion was wired into L1 only since round 22 —
purely an accident of L1 being the first fetcher built, not a deliberate
choice that L2/L3 shouldn't have it. A page that had to escalate past L1
(the majority of "hard" targets — the entire reason L2/L3 exist) lost
markdown entirely regardless of configuration. `AdaptiveSelector`
extraction hit this exact same bug shape in round 28 (wired into L1-
equivalent thinking, fixed by centralizing in the worker) — markdown gets
the identical fix for the identical reason, right next to it in the same
function.

**Alternatives considered:**
- Add the same three call sites to `Level2Fetcher`/`Level3Fetcher` too.
  Rejected — triples the maintenance surface (3 fetchers × however many
  internal code paths each has) for logic that has nothing to do with any
  individual fetcher's job (fetching HTML), and was already duplicated
  three times *within* L1 alone before this fix.
- Keep it fetcher-owned but inject a shared converter via the factory.
  Rejected — still requires every fetcher to remember to call it and
  every internal success path within each fetcher to remember it too
  (`level_1.py` alone had 3 such paths); centralizing in the one place
  that already knows "this URL's fetch just succeeded, at this level"
  removes the possibility of a missed call site entirely.

**Status:** Active. Firecrawl itself was also generalized in the same
round (`services/firecrawl_client.py` — `FIRECRAWL_BASE_URL` for
self-hosted instances, API key no longer required) but that's a separate,
independent decision from where the call site lives.

---

## Decision: Debounced Redis Kick + Pub/Sub, Not Pub/Sub Alone

**Date:** 2026-08-14 | **Round:** 34

**What:** `ProxyManager.get_proxy`'s exhaustion path does a debounced
`SET proxy:harvest:kick NX EX 30` on Redis; only the caller that actually
creates the key (i.e. no kick already pending) also `PUBLISH`es to
`proxy:events:exhausted`. `proxy/harvester_daemon.py` reacts via a ~5s-poll
watcher task that checks the *key*, not a pub/sub subscriber.

**Why:** Redis pub/sub is at-most-once and has no replay — a message
published while the harvester daemon is mid-restart, mid-deploy, or
momentarily busy is gone forever, and the pool would then wait out the
full steady-state timer (up to 10 minutes) despite a real signal having
fired. A poll against a durable key can't miss a signal that way: the key
persists (30s TTL) regardless of whether the daemon was listening at the
exact publish instant. The `PUBLISH` is kept anyway as a low-latency path
for the common case (daemon already running, not mid-restart) — the
key-poll is the reliability backstop, not a redundant leftover.

**Alternatives considered:**
- Pub/sub only, no key. Rejected — the missed-restart failure mode above
  is exactly the class of bug this whole round exists to close (proxy
  pool not self-healing); a signal mechanism with a silent-miss window
  would just relocate the bug, not fix it.
- Poll the key only, no pub/sub. Considered acceptable (the key alone is
  sufficient for correctness — the watcher would catch it within one 5s
  poll interval regardless) but keeping `PUBLISH` costs nothing and
  shaves a few seconds of latency in the common case, so both stayed.

**Status:** Active. Same pattern reused deliberately for
`proxy/dlq_reaper.py`'s eligibility checks — poll current
state (`pool_health.py::current_state`, `CircuitBreaker.state()`) rather
than wiring a push-only "pool recovered" event, for the identical
missed-signal-on-restart reason.

---

## Decision: `webhook_dispatch.py` Split From `orchestrator/tasks.py`

**Date:** 2026-08-14 | **Round:** 34

**What:** The durable webhook-delivery function
(`enqueue_and_deliver_webhook_event`) lives in a new
`orchestrator/webhook_dispatch.py`, not inside `orchestrator/tasks.py`
where the per-job caller (`_dispatch_job_webhook`) lives.

**Why:** `tasks.py` runs bootstrap side effects at *module import time* —
`load_config()`, `bootstrap_observability()`, `configure_budget()` —
meant to execute exactly once per rq work-horse process. `proxy/
harvester_daemon.py`'s pool-health cycle needs the same durable-dispatch
function for its own ops-channel alerts (Phase C/B of this round), but
that daemon is a different long-lived process with its own
`bootstrap_observability()` call already in its `run()`. Importing
`tasks.py` from `harvester_daemon.py` — even a deferred, in-function
import — would still execute `tasks.py`'s module-level code on first
import, double-bootstrapping observability/tracing/budget semaphores
inside the proxy daemon process with a *different* config object than the
one it already initialized with. Splitting the shared function into a
module with no import-time side effects removes the hazard entirely
rather than working around it (e.g. with a guard flag).

**Alternatives considered:**
- Guard `tasks.py`'s module-level bootstrap with an `if not
  _already_bootstrapped` flag so a second import is a no-op. Rejected —
  papers over the real issue (two unrelated processes sharing one
  module's import-time side effects) instead of removing the coupling,
  and the flag itself would need to be process-global state that's easy
  to get wrong under module-reload edge cases (tests, `importlib.reload`).
- Duplicate the dispatch function in `harvester_daemon.py`. Rejected —
  the whole point of `webhook_outbox`/Slack-formatting/retry-backoff is
  one delivery mechanism for both job events and pool-health events (see
  the Phase C summary in `technical-debt.md`'s round-34 entry); a
  duplicate would drift the moment either copy changed.

**Status:** Active.

---

## Decision: DLQ Auto-Retry Eligibility Reads Pure State, Never Mutates It

**Date:** 2026-08-14 | **Round:** 34

**What:** `proxy/dlq_reaper.py`'s `CIRCUIT_OPEN` eligibility check calls
`CircuitBreaker.state(domain)` (a pure Redis read) instead of
`CircuitBreaker.allow_request(domain)` (the method the real fetch path
uses, which *transitions* an `OPEN` circuit to `HALF_OPEN` once its
cooldown has elapsed and treats that as permission to probe).

**Why:** `allow_request()` is written for exactly one real probe attempt
to consume — a `HALF_OPEN` circuit closes on that probe's success or
re-opens on its failure. The reaper isn't a real fetch attempt; it's
bookkeeping deciding whether to re-enqueue a job that will *itself* make
real fetch attempts later, through the normal `Worker.process_job` path,
which already calls `allow_request()` correctly. If the reaper's
eligibility check called `allow_request()` instead, it would silently
consume the one HALF_OPEN probe slot meant for real traffic — a domain
recovering from an open circuit could have its single probe opportunity
eaten by the reaper's polling cycle instead of an actual scrape attempt,
with no observable difference from the reaper's point of view but a real
cost to the fetch path's own recovery logic.

**Alternatives considered:**
- Call `allow_request()` and treat any `True` result (including the
  HALF_OPEN-probing case) as eligible. Rejected for the probe-consumption
  reason above — also would have made the reaper strictly more
  "optimistic" than the fetch path itself, retrying jobs the breaker
  hasn't actually confirmed healthy yet.

**Status:** Active. `PROXY_EXHAUSTED` eligibility follows the same
pure-read principle for consistency — `pool_health.py::current_state()`
reads the last-persisted per-tier state without triggering a fresh
Postgres recompute, which is `PoolHealthMonitor.check()`'s job on its own
schedule, not the reaper's.

---

## Decision: `partial_failure` as an Additive Boolean, Not a New `JobStatus`

**Date:** 2026-08-14 | **Round:** 34

**What:** `JobStatusResponse` gained `partial_failure: bool = False`
instead of a new `JobStatus.COMPLETED_WITH_ERRORS` enum value. `status`
itself keeps its existing five values and existing semantics unchanged.

**Why:** The bug being fixed (a job with a DLQ'd URL alongside a
succeeded one reports plain `COMPLETED`, indistinguishable from a fully
clean run) needs a machine-readable signal, but `JobStatus` is a DB CHECK-
constraint enum (`migrations/versions/001_initial.py`) with unknown fan-
out across every existing consumer that branches on `status.value ==
"COMPLETED"` — the webhook payload, any external integration polling
`GET /v1/jobs/{id}`, and this codebase's own `orchestrator/tasks.py`
metric-status bucketing (`"completed" if status == JobStatus.COMPLETED
else "failed"`). A new enum value changes what "COMPLETED" branches match
against everywhere without touching those call sites' logic — the classic
enum-widening hazard. An additive field carries the same information with
zero blast radius: existing `status`-only consumers keep working exactly
as before, and a consumer that cares about the distinction now has an
explicit, unambiguous place to look instead of having to enumerate a
wider set of "successful" enum values forever after.

**Alternatives considered:**
- `JobStatus.COMPLETED_WITH_ERRORS` new enum value. Rejected for the
  blast-radius reason above.
- Infer partial failure from `error` being non-null while `status ==
  COMPLETED`. Rejected — `error` is a semicolon-joined string built for
  human reading, not a stable contract; inferring structured meaning from
  its presence/absence is fragile compared to a dedicated field, and ties
  future changes to `error`'s formatting to this unrelated concern.

**Status:** Active. Computed identically in two places —
`Worker.process_job` (in-memory, the value a webhook payload is built
from) and `api/routes.py::get_job` (reconstructed from `scrape_results`
rows on every poll) — because they build `JobStatusResponse` from
different sources and don't share a code path; kept in sync by using the
exact same formula (`bool(errors) and any(r.success for r in results)`)
in both, not by extracting a shared helper across two otherwise-unrelated
functions.

---

## Decision: extraction-engine Integration Is Opt-In and Fails Soft

**Date:** 2026-08-03 | **Round:** pre-34 (ported from `.wolf/cerebrum.md`'s
Decision Log during the round-34 knowledge audit — recorded there at the
time but never mirrored here, the project's own authoritative WHY-log;
see the "OpenWolf ↔ `.claude/knowledge/` Division of Labor" note at the
end of this file for why that gap existed and how it's closed going
forward)

**What:** `Worker.process_job` only calls `services/
extraction_engine_client.py` when `EXTRACTION_ENGINE_BASE_URL` is set AND
a real `extraction_schema` was supplied on the request. If the client
call fails for any reason, it never raises — only returns `None` — and
the job falls back to `AdaptiveSelector` rather than the extraction being
lost.

**Why:** Mirrors `services/firecrawl_client.py`'s already-established
contract in this codebase: an external enrichment service is an
enrichment, not a hard dependency. Chosen over inventing a new
error-handling shape for this one integration — one consistent pattern
for "optional external service that improves output when configured/
reachable, never blocks the pipeline when it isn't" across both
integrations, rather than two different failure philosophies a reader
has to learn separately.

**Alternatives considered:**
- A distinct error-handling/retry shape specific to extraction-engine
  (e.g. raising and letting the caller decide). Rejected — no other part
  of this codebase treats an optional external enrichment service as a
  hard dependency, and doing so here would be the one inconsistent case.

**Status:** Active.

---

## OpenWolf ↔ `.claude/knowledge/` Division of Labor (Round 34)

**Context:** A round-34 knowledge audit found `.wolf/cerebrum.md` (a
separate, OpenWolf-tool-maintained memory file, auto-updated and
`@`-imported into every session via `CLAUDE.md`) has its own "Decision
Log" section, explicitly scoped to the same WHY/alternatives/tradeoffs
purpose as this file. They had already drifted: the extraction-engine
decision above existed only in `cerebrum.md` for 11 days before this
audit caught it and ported it here.

**Going forward:** `cerebrum.md`'s Decision Log is OpenWolf's own
session-memory mechanism — do not edit it by hand (its own header says
so; `openwolf scan`/the OpenWolf daemon own its lifecycle) and do not
try to merge the two systems into one. Instead: any decision logged to
`cerebrum.md`'s Decision Log that has real lasting architectural
consequence (not a one-off environment gotcha — those belong in
`cerebrum.md`'s separate "Do-Not-Repeat" section and are fine to stay
OpenWolf-only) should ALSO be written here, in this file, in this file's
format, in the same session it's made. `cerebrum.md` stays the fast
session-local capture; this file stays the permanent, cross-referenced,
project-authoritative record `CLAUDE.md`'s Navigation section actually
points readers to. A future knowledge-maintainer/audit pass should
re-check `cerebrum.md`'s Decision Log against this file's coverage
periodically, the same way this round did.

---

## Decision: Keep Both Pool-Health Alert Paths — Intentional, Not Duplicate

**Date:** 2026-08-14 | **Round:** 34 (follow-up, resolved during the
round-34 knowledge audit)

**What:** Both the pre-existing `ProxyPoolCriticallyLow` Prometheus/
Alertmanager rule (`monitoring/alerts/prometheus_rules.yml` →
`SLACK_WEBHOOK_URL`) and the new `proxy/pool_health.py` →
`config.webhook.ops_webhook_url` event-driven path stay. `config/
base.yaml` now documents the relationship and recommends pointing
`ops_webhook_url` at a distinct Slack channel from `SLACK_WEBHOOK_URL`.

**Why:** Investigated as an open thread rather than left unresolved. The
two paths fail independently, which is the actual argument for keeping
both rather than picking one: Alertmanager's rule needs Prometheus
scraping + a 5-minute sustained-condition window and goes dark if this
app's own Redis/Postgres/webhook-outbox pipeline is what broke;
`pool_health.py`'s path needs that same app pipeline healthy and goes
dark if Prometheus/Alertmanager themselves are misconfigured or down.
Each covers the other's blind spot. `pool_health.py`'s path also adds
information Alertmanager's rule structurally can't — per-tier state
(Alertmanager's rule is pool-wide only) and the specific old→new
transition, not just "below threshold." Since `ops_webhook_url` defaults
to unset, there was zero live duplication in production before this
decision — the overlap was a latent risk (both firing to the same
channel if an operator configured them identically), not an active one,
which is why the resolution is operator guidance + documentation rather
than a code change to either path.

**Alternatives considered:**
- Retire the Alertmanager rule, rely solely on the new path. Rejected —
  loses the "keeps working when this app's own delivery pipeline is the
  thing that's broken" property, which is exactly the failure mode a
  proxy-pool-health alert most needs to survive.
- Retire the new `pool_health.py`/`ops_webhook_url` path, rely solely on
  Alertmanager. Rejected — this was round 34's actual fix for the
  original user complaint ("why doesn't Slack tell me the pool is
  exhausted") and has properties (per-tier granularity, no 5-minute
  sustained-condition delay, survives Prometheus/Alertmanager outages)
  the Alertmanager rule doesn't have; removing it would reopen the
  original gap for those specific failure modes.
- Merge into one mechanism (e.g. have `pool_health.py` write into the
  same `proxy_pool_validated_count` gauge Alertmanager already watches,
  and drop the webhook-outbox path entirely). Rejected — collapses back
  to a single point of failure (Prometheus/Alertmanager), the exact
  property the "keep both" reasoning above argues against; the marginal
  engineering cost of two independent paths is worth the resilience for
  a signal this operationally important.

**Status:** Active. Revisit only if operator feedback shows the two
paths cause real double-alert confusion in practice despite the
distinct-channel guidance — that would be evidence-based grounds to
reconsider, not a reason to preemptively merge them now.

---

## Decision: ReverseDnsAsnClassifier Over Fixing the MaxMind Wiring

**Date:** 2026-08-14 | **Round:** 35

**What:** `proxy/asn_classifier.py::MaxMindAsnClassifier` (env-gated on
`GEOIP_ASN_DB_PATH`, a downloaded GeoLite2-ASN database) was deleted
outright and replaced with `ReverseDnsAsnClassifier` — a DNS PTR-hostname
lookup (`loop.getnameinfo()`) matched against the same
`_DATACENTER_KEYWORDS`/`_MOBILE_KEYWORDS` lists MaxMind's org-name field
would have used. `build_asn_classifier()` now unconditionally returns it;
there's no env gate or "inert until configured" branch anymore.

**Why:** Root-caused as part of the round-35 `proxy_exhausted`
investigation (see `technical-debt.md`): `GEOIP_ASN_DB_PATH` had never
been set on any deployment of this repo, so `MaxMindAsnClassifier` had
never actually run in production — every proxy scored `asn_class=
"unknown"` forever, permanently zeroing `scoring.py`'s 10-point
`ASN_BONUS` dimension. The obvious fix was wiring up MaxMind properly
(sign up, generate a license key, download the `.mmdb`, keep it
refreshed). **User explicitly rejected that path mid-session** — "don't
want scraper too dependent on external like maxmind, come up with
another option" — on the grounds that a third-party account + license
key + a database file an operator has to remember to refresh is exactly
the kind of dependency that silently rots (which is precisely what had
just happened: the feature existed in code since round 22 and had never
once been exercised). Reverse-DNS needs no account, no key, no file —
just a standard DNS lookup already available wherever the harvester has
network access, which it needs anyway to validate proxies over HTTP.

**Alternatives considered:**
- **Fix the MaxMind wiring** (add `GEOIP_ASN_DB_PATH` to `.env.example`,
  document the MaxMind signup flow, bind-mount the `.mmdb` into the
  container). Rejected per the user's explicit direction above — also
  the most precise option technically (a maintained IP-to-ASN database
  beats a hostname heuristic), but the precision wasn't worth the
  operational dependency for this use case.
- **RIR delegation files / Team Cymru bulk IP-to-ASN tables** (free,
  no-signup alternatives to MaxMind). Considered and not pursued —
  still an external file to download and refresh periodically, doesn't
  remove the "operator has to remember to maintain something" problem
  the user was actually objecting to, just changes the vendor.
- **Drop ASN classification entirely**, accept the 10-point `ASN_BONUS`
  dimension staying permanently zero. Rejected — the whole point of the
  investigation was that the dimension being permanently zero (via
  `NullAsnClassifier`) was contributing to the pool's score ceiling
  sitting under `min_score_level_2`'s threshold; removing the dimension
  entirely doesn't fix that, it just changes the math slightly
  differently (see `scoring.py`'s weight-redistribution logic for
  `success_rate=None`, same shape).

**Trade-off accepted:** less precise than a maintained IP database — some
datacenters don't set a descriptive PTR record (score understated), some
residential ISPs do set one containing an ISP/provider name that happens
to match a keyword (score overstated). Judged acceptable because the
scoring dimension only needs to move proxies off a permanent zero, not be
perfectly accurate, and the keyword lists already existed and needed no
new tuning to reuse against a different data source.

**Status:** Active.

---

## Decision: Self-Healing Daemons Consolidated Into One Supervised Container

**Date:** 2026-08-14 | **Round:** 35

**What:** `proxy-harvester`, `dlq-reaper`, and `webhook-sweeper` — each a
separate `docker-compose.yml` service since round 34 — were collapsed
into the `api` container, run as supervised subprocesses via
`supervisord` (new `docker/supervisord.conf`, `Dockerfile`'s `CMD`
changed from bare `uvicorn` to `supervisord -c
/etc/supervisor/supervisord.conf`). `worker-l1/l2/l3` were deliberately
left as separate compose services.

**Why:** Root-caused as part of the same `proxy_exhausted` investigation:
the 3 daemons existed and worked, but nobody was starting them on this
deployment — `docker compose ps` showed only `api`/workers/infra running,
and CLAUDE.md's documented dev bring-up command
(`docker compose up -d postgres redis pgbouncer minio migrate`) never
named them. The proxy pool went stale (`last_validated` ~64h old,
maxing out `scoring.py`'s recency penalty) with zero operator-visible
signal that anything was missing — `docker compose ps` just didn't show
rows for services an operator wouldn't necessarily know to look for.
**User explicitly directed this architecture** over the alternative of
just fixing the documented bring-up command: "when the scraper engine
container is started all dependent or related containers start as well,
without crashes... come up with a robust and resilient way to do this" —
i.e. starting the API should be sufficient by construction, not
contingent on an operator remembering a longer service list correctly
every time.

**Alternatives considered:**
- **Fix the documented bring-up command** (add the 3 service names to
  CLAUDE.md's Quick Commands, or tell operators to run a bare
  `docker compose up -d` with no service list). Simpler, smaller diff,
  no new dependency (`supervisor`) or crash-isolation logic needed.
  Rejected per the user's explicit direction — still relies on an
  operator following documentation correctly, which is exactly the
  failure mode that caused this incident in the first place.
- **Keep them as separate containers, add `restart: unless-stopped` to
  each.** Doesn't satisfy "starting the api container starts everything"
  — an operator running `docker compose up -d api` in isolation (e.g.
  scripted deploys, `docker run` outside compose) still gets a pool that
  never refills. Rejected for the same reason as above.
- **Merge `worker-l1/l2/l3` in too**, one giant supervised container for
  every long-running process. Rejected — workers are the horizontally-
  scaled compute/browser workhorses (3 replicas of the same rq consumer,
  independently scaled via `docker-compose.yml` replica count in
  practice); the self-healing daemons are lightweight singleton loops.
  Different scaling units belong in different containers even under this
  "start together" requirement — the requirement was about the daemons
  an operator might forget to start, not about workers, which are
  already visibly present in `docker compose ps` and not the thing that
  silently went missing.

**Trade-off accepted:** `docker exec <container> supervisorctl status`
replaces `docker compose ps` as the way to check these 3 processes'
health — a real, if minor, discoverability cost documented in
`operations.md`. The `/v1/health` container healthcheck only reflects
`api`'s own Postgres/Redis reachability, not each daemon's individual
liveness — extending `/health` with per-daemon status was judged out of
scope for this round (see `technical-debt.md`).

**Status:** Active. Verified live: rebuilt image, all 4 supervisord
programs `RUNNING`; killed `proxy-harvester`'s PID directly and confirmed
supervisord restarted it (new PID) within ~6s while `api`/`dlq-reaper`/
`webhook-sweeper` and the `/v1/health` check stayed up throughout.

---

## Decision: Heartbeat-via-Redis Over Supervisor RPC for Daemon Liveness

**Date:** 2026-08-14 | **Round:** 36

**What:** `/v1/health`'s new daemon-liveness check (`api/health.py::
_check_daemon_liveness`) reads Redis heartbeat keys that
`core/periodic.py::run_periodic` writes after every cycle attempt, rather
than querying supervisord's XML-RPC socket (`/tmp/supervisor.sock`, the
same interface `supervisorctl` itself uses) for each program's state.

**Why:** Two considered mechanisms, both technically available since
round 35 put all 3 daemons under one supervisord instance:
1. **Supervisor RPC** — ask supervisord directly "is `proxy-harvester`
   `RUNNING`." Rejected: only proves the OS process exists, not that its
   loop is making progress — a process hung on a slow/blocked call
   (stuck DB query, network call that never times out) still reads
   `RUNNING` to supervisorctl, so this wouldn't have caught the exact
   failure mode round 35 was fixing (a harvester that technically hadn't
   crashed, just stopped doing useful work). It also hard-couples the
   health check to this exact container topology — which has already
   changed once in this repo's history (standalone containers → one
   supervised container, round 35) — meaning a future topology change
   would silently break this check again.
2. **Heartbeat-via-Redis** (chosen) — each periodic job writes its own
   "I ran" timestamp after every cycle attempt. Answers the more
   meaningful question directly ("did this loop actually run recently"),
   is decoupled from container topology entirely (works the same whether
   the 3 daemons are one container, three, or something else later), and
   reuses this codebase's own established pattern (`proxy/manager.py`'s
   debounced kick key, `redis.raw` for system-level non-tenant keys) —
   no new dependency, no new client library.

**Trade-off accepted:** a heartbeat only proves the *event loop*
scheduled that coroutine recently — a job that yields control properly
(e.g. `await`s a slow-but-not-hung network call) still ticks other
cooperative tasks including an unrelated one's heartbeat write, so this
doesn't detect every possible partial-hang scenario. Judged sufficient:
it correctly detects the two failure modes that actually matter operationally
(process crashed / process fully deadlocked, both stop the loop from
reaching the heartbeat write at all) without the topology coupling or
false confidence of "process exists" that supervisor RPC would have
given.

**Status:** Active.

---

## Decision: Daemon Liveness in `/v1/health` Is Informational, Not Status-Affecting

**Date:** 2026-08-14 | **Round:** 36

**What:** A stale/dead daemon (heartbeat key expired) is surfaced via new
`daemons`/`checks["daemons"]` fields in `/v1/health`'s response, but does
**not** flip `HealthStatus.healthy` or the endpoint's HTTP status code
(200 stays 200 even with every daemon stale). This reverses the initial
implementation, which folded it into the same `healthy` flag pg/redis/s3
already use.

**Why:** Caught by a real, pre-existing test failure during
implementation, not a hypothetical — `tests/integration/test_api_main.py::
TestCreateApp::test_lifespan_wires_dependencies_and_instruments_tracing`
creates a real app against a real Redis and asserts `GET /v1/health`
returns 200. With the first (status-affecting) version, this started
failing with 503, because no daemon in that Redis had ever written a
heartbeat. Two real, legitimate scenarios both produce this same
"no heartbeat yet" state: (1) **any fresh deploy** — `promotion`'s
900-second default interval means up to 15 minutes pass before its first
heartbeat exists, during which a status-affecting check would have
reported the whole `api` as unhealthy despite pg/redis/s3/the API itself
being completely fine; (2) **`api` run standalone**, as this exact
integration test does — no co-located daemons at all, by design (it's
testing `create_app()`'s lifespan wiring, not the daemons). A health
check that false-positives on ordinary startup timing or a legitimate
standalone-testing topology is itself a robustness bug, not the
robustness improvement this round was asked to deliver.

**Alternatives considered:**
- **Keep it status-affecting, add a startup grace period** (track
  `api`'s own process-start time, don't evaluate daemon liveness until
  enough wall-clock time has passed for the slowest job to plausibly have
  run once). Rejected — doesn't fix scenario (2) at all (standalone `api`
  with daemons that will *never* run), adds real complexity (a second
  timing concept, config plumbing for "how long is long enough"), and the
  informational-only version already gives an operator everything they
  need to know *which* daemon is stale without the false-alarm risk.
- **Status-affecting, but only for daemons the deployment topology
  claims to expect** (e.g. an env var declaring "this api instance always
  has 3 co-located daemons"). Rejected as unnecessary config surface for
  a problem the informational field already solves — an operator (or an
  automated system) that cares about daemon liveness specifically reads
  the `daemons` field; a passive 200-vs-503-only monitor was never going
  to distinguish *which* daemon died anyway, so losing that distinction
  isn't a real loss for that audience.

**Precedent this follows:** the same file already treats S3 as optional —
`s3_reachable = True` unconditionally when `s3` isn't configured, rather
than failing health on an intentionally-absent dependency. Daemon
liveness during a startup window (or in a topology where daemons
genuinely aren't present) is the same shape of "absent isn't the same as
broken."

**Status:** Active. Verified live on the actual dev deployment: stopped
`webhook-sweeper` via `supervisorctl`, confirmed `/v1/health` reported it
`"stale (webhook_sweep)"` while `status` stayed `"ok"` (200) throughout;
restarted it, confirmed recovery to `"healthy"` after one cycle.

## Decision: TCP+HTTPS-CONNECT Preflight, Not Full HTTP Validation, Before Leasing a Proxy

**Date:** 2026-08-14 | **Round:** 37

**What:** `ProxyManager.get_proxy()` now runs a two-stage preflight
(`proxy/net_probe.py::lease_preflight`) on a candidate proxy immediately
before returning it as a lease, inside the existing `MAX_ATTEMPTS=5` retry
loop: a TCP connect (2.0s timeout), then — only if that passes — one real
GET of an HTTPS URL through the proxy (httpx issues this as a CONNECT
tunnel). A failure at either stage is treated exactly like a real
post-fetch failure (`mark_failure` — domain-ban + score recompute) and the
loop moves to the next candidate. Shipped in two steps within the same
round: TCP-only first, then upgraded to add the HTTPS-CONNECT stage after
live re-testing showed TCP-only wasn't sufficient (see Why). The TCP
connect logic itself was extracted from `ProxyHarvester._tcp_probe` into
shared `proxy/net_probe.py::tcp_probe`, used by both the harvester's own
candidate pre-filter and this lease-time check, instead of being
duplicated.

**Why:** round 35 fixed the scoring bug that made L2/L3 promotion
structurally impossible, which — correctly — unblocked real proxies being
leased. That exposed a gap nobody had reason to notice before: nothing
validated a leased proxy before handing it to the real fetch, so a
dead-but-promoted free proxy cost the caller a full 40-60s browser
navigation timeout per level instead of failing fast. Live-tested and
confirmed as the root cause of a user-reported regression (150s+ job
hangs) — see `technical-debt.md`'s round-37 entry for the full evidence
trail.

The HTTPS-CONNECT stage was added the same round after the TCP-only
version's own live re-verification caught a real gap: a real `/v1/scrape`
job still failed with worker logs showing `"Connection to remote host was
lost"` — a proxy that passed the TCP check but didn't actually forward
traffic, and separately, a proxy that passed a plain-HTTP check but then
failed `"Tunnel connection failed: 400 Bad Request"` on the real HTTPS
fetch (confirmed by directly re-probing that same proxy with an HTTPS URL
and reproducing the same failure). Plain HTTP validates the wrong thing —
almost everything L2/L3 needs a proxy for (real target pages, Camoufox's
own `geoip=True` launch-time IP lookup) is HTTPS.

**Alternatives considered:**
- **Full HTTP-through-proxy validation at lease time** (reusing
  `harvester.py`'s existing `_http_validate`, which retries across
  multiple judge URLs and classifies anonymity). Rejected — `_http_validate`'s
  own docstring already explains why: it's "acceptable since this only
  runs from already-bounded-concurrency contexts... never a request-path
  hot loop." Running it on every single L2/L3 lease would add that cost to
  every *successful* fetch too, not just the failing ones — a worse
  trade for a lease-time hot path than TCP+one-HTTPS-GET, which already
  catches both the dominant failure mode (`ConnectTimeout`/`ConnectError`)
  and the CONNECT-tunnel-capability gap at near-zero cost to the happy
  path, without anonymity classification or multi-URL retry overhead this
  hot path doesn't need.
- **No preflight; instead tighten each level's own navigation timeout.**
  Rejected — doesn't fix the root cause (a dead proxy is still tried for
  real, just for a shorter fixed window), and shortening L2/L3's timeouts
  globally risks cutting off genuinely slow-but-working real fetches
  (challenge-solving pages in particular lean on `max_total_wait_ms`),
  trading one failure mode for another instead of removing it.
- **A total per-URL wall-clock budget across all 3 levels**, enforced in
  `orchestrator/worker.py`. Considered as defense-in-depth but scoped out
  — the preflight already bounds the dominant failure mode
  (`ConnectTimeout`/`ConnectError`, the majority of observed failures) to
  ~10s worst case; adding a second, independent timeout mechanism for the
  smaller residual case (a proxy that connects but doesn't forward
  traffic — `ReadTimeout`/`BrokenResourceError`) wasn't justified by the
  evidence gathered this round. Left as a documented, accepted residual,
  not silently dropped — worth reconsidering if that residual case turns
  out to matter in practice.

**Precedent this follows:** `mark_failure`'s existing self-healing loop
(domain-ban + `ScoringEngine` recompute) was already the mechanism for
"a proxy that keeps failing gets deprioritized and eventually evicted" —
this decision triggers that same mechanism earlier (at preflight) instead
of inventing a second one.

**Status:** Active. Suite (final, after all round-37 layers): 804 passed,
100.00% coverage. Live-verified on the actual dev deployment across
multiple rebuild/redeploy cycles as each layer was added — see
`technical-debt.md`'s round-37 entry for the complete evidence trail,
including the final clean measurement (92%, 11/12 real proxied fetches)
and an honestly-recorded test-methodology confound from this round's own
repeated same-domain testing.

## Decision: SQL-Side Candidate Exclusion, Not Python-Side Filtering of a Fixed Top-20

**Date:** 2026-08-14 | **Round:** 37

**What:** `ProxyManager._select_candidate()`'s query now takes the
`exclude` set (candidates already tried within the current `get_proxy()`
call) as a SQL parameter — `AND NOT (ip || ':' || port = ANY($2::text[]))`
— instead of fetching a fixed `LIMIT 20` top-scored slice and filtering
`exclude` only in Python afterward.

**Why:** live-caught while re-verifying the preflight fix above: a domain
scraped repeatedly in a short window (this round's own testing against
`httpbin.org`) accumulated domain-bans across enough of its top-20-by-score
proxies that a fresh `get_proxy()` call for that exact domain exhausted in
~1 attempt — `_select_candidate` kept re-fetching the SAME stale top-20
rows every attempt within the call, and once ~20 of them were excluded
(banned and/or preflight-failed earlier in the same call), it returned
`None` regardless of how many more viable, lower-ranked candidates existed
in the pool overall (confirmed: 50+ score-eligible proxies existed while
this was happening). Verified directly against real Postgres that the new
clause correctly drops excluded IPs from the result and that an empty
exclude array (the common case — first attempt in a call) correctly
returns everything unfiltered.

**Alternatives considered:**
- **Just raise `LIMIT 20` to a bigger constant.** Rejected — delays the
  same problem rather than removing it; any fixed limit re-fetches the
  same static slice every attempt regardless of how it grows, so a domain
  hit hard enough will eventually exhaust any fixed window.
- **Materialize domain-bans into Postgres so they can be excluded in the
  same query as reliability_score.** Rejected as unnecessary complexity —
  bans live in Redis by design (TTL-based expiry is exactly what Redis is
  for), and the `exclude` set already available in Python (built from
  `_select_candidate`'s own return values across attempts) is sufficient
  to solve the actual observed bug without a second ban-tracking system.

**Status:** Active. Unit-tested (`test_second_attempt_excludes_first_candidate_in_sql`)
and confirmed against real Postgres (exclusion clause + empty-array case
both verified with real queries against the live `proxy_pool` table).

## Decision: One Same-Level Retry With a Fresh Proxy on Proxy-Attributable Fetch Failures

**Date:** 2026-08-14 | **Round:** 37

**What:** `orchestrator/worker.py` gained `_fetch_with_proxy()`, a shared
L2/L3 lease-fetch-score helper (replacing near-duplicate inline blocks)
with one bounded retry (`_SAME_LEVEL_PROXY_RETRIES = 1`): if a fetch fails
with a category in `_PROXY_RETRYABLE_CATEGORIES`
(`FailureCategory.BROWSER_CRASH`, `FailureCategory.NETWORK_TIMEOUT`), it
leases a *fresh* proxy and retries once before giving up on that level.
Non-retryable categories (e.g. `DETECTION_BLOCK`, a content/page-level
failure) return immediately, unchanged from before — retrying with a
different proxy wouldn't plausibly fix those.

**Why:** even with the lease-time preflight (both decisions above),
`_fetch_url` only ever leased ONE proxy per level — a proxy that passed
preflight could still fail once handed to the real browser fetch, for a
reason the preflight can't predict, burning the entire level on that one
unlucky proxy despite 50+ other viable candidates sitting in the pool.
Root-caused live: Camoufox's own `geoip=True` (default, wired into the
real `BrowserPool` via `orchestrator/tasks.py`) makes its own out-of-band
IP lookup at browser launch, trying 6 different third-party services
internally (`camoufox/ip.py::public_ip`). A proxy that passed our
HTTPS-CONNECT preflight against one judge endpoint sometimes failed all 6
of Camoufox's internal targets anyway — different destinations than our
probe, and free proxies can have per-destination routing quirks unrelated
to general CONNECT capability. Confirmed directly: a standalone
`Level3Fetcher.fetch()` call with a preflight-passing proxy raised
`FailureCategory.BROWSER_CRASH` with `"Failed to get IP address: ..."`.

**Alternatives considered:**
- **Point the preflight at Camoufox's exact internal IP-check targets
  too.** Rejected — fragile and version-coupled (Camoufox could change its
  internal service list any release), and Camoufox already retries across
  6 services internally and still failed all 6 in the observed case,
  suggesting proxy-specific flakiness rather than a single fixable target
  to add to the preflight.
- **Disable `config.camoufox.geoip` globally.** Rejected — `geoip` is
  explicitly part of design invariant §1.1.2 ("Camoufox owns 100% of
  fingerprint/geoip/UA/canvas/WebGL surface") and provides a real
  anti-detection signal (browser timezone/locale matching the proxy's exit
  IP). Silently disabling it to dodge this failure mode would trade away
  anti-detection quality without being asked to, for a problem the retry
  approach solves without touching the invariant at all.
- **Retry indefinitely / retry every failure category.** Rejected —
  unbounded retries against a finite free-proxy pool risk exactly the
  self-DOS `proxy/promotion.py`'s own docstring already warns against for
  a different subsystem ("~0.02% HTTP-forwarding success rate on free
  proxies means unbounded retries are self-DOS"); retrying non-proxy
  failure categories (detection, content parsing) burns a lease for a
  failure a different proxy can't plausibly fix.

**Status:** Active. Unit-tested (`test_level2_retryable_failure_then_success_uses_fresh_lease`,
`test_level2_non_retryable_failure_category_gives_up_immediately`, and the
updated `test_level{2,3}_real_fetch_failure_marks_failure_not_success`
asserting 2 attempts for a persistently-failing retryable category).
Live-verified the underlying failure category is real and reachable
(directly reproduced `BROWSER_CRASH` against a preflight-passing proxy);
the retry path's live exercise was confounded by a same-domain
test-methodology artifact (see `technical-debt.md`'s round-37 entry) —
correctness is established by the passing unit tests, not further chased
live given the confound.

## Decision: Camoufox Geoip-Launch Failures Get One Retry Without Geoip, Not a Global Disable

**Date:** 2026-08-14 | **Round:** 37

**What:** `browser/camoufox_wrapper.py::CamoufoxWrapper` gained
`_launch_with_geoip_fallback()`: if `AsyncCamoufox.__aenter__()` raises
`camoufox.exceptions.InvalidIP` and `self._geoip` was `True`, retry the
launch once with `geoip=False`, same proxy, logging a warning. Any other
exception, or a second `InvalidIP` with geoip already off, propagates
unchanged.

**Why:** even with the lease-time preflight (TCP+HTTPS-CONNECT, both
decisions above), a proxy could still crash the browser launch entirely.
Root-caused: Camoufox's own `geoip=True` (production default) makes an
out-of-band IP lookup at launch, trying 6 different third-party services
internally (`camoufox/ip.py::public_ip`) — none of which are the proxy's
own reachability to the real target site. Confirmed directly: a
preflight-passing proxy (validated against our own HTTPS judge endpoint)
still failed all 6 of Camoufox's internal targets, raising `InvalidIP`.
Across three separate live measurement runs (fresh-domain-per-trial
methodology, no code changes between runs), success bounced between 92%,
75%, and 100% — variance consistent with transient per-run proxy/service
flakiness, not a deterministic defect this fix, or any fix, can fully
eliminate; the fallback reduces how often a single bad geoip check burns
an otherwise-usable proxy.

**Alternatives considered:**
- **Disable `config.camoufox.geoip` globally.** Rejected — `geoip` is
  explicitly named in design invariant §1.1.2 ("Camoufox owns 100% of
  fingerprint/geoip/UA/canvas/WebGL surface") and provides a real
  anti-detection signal (browser timezone/locale matching the proxy's exit
  IP). A global disable would sacrifice that for every session, not just
  the ones that actually hit this specific failure mode.
- **Point the preflight (net_probe.py) at the same 6 services Camoufox
  checks internally.** Rejected — fragile and version-coupled (Camoufox
  could change its internal service list any release without our
  knowledge), and Camoufox already tries all 6 internally and still failed
  in the observed case, so duplicating that check in the preflight
  wouldn't have caught it any earlier — the failure is inherently only
  knowable at actual launch time.

**Status:** Active. 5 new unit tests (`AsyncCamoufox` mocked directly at
`camoufox.async_api.AsyncCamoufox` — no prior test in this file exercised
the real launch path with a controllable mock): happy path, fallback
success, geoip-already-off re-raise, non-`InvalidIP` propagation
unchanged, and `__aenter__`'s semaphore-release contract holding through
the refactor. Note: `browser/*` is excluded from the CI-enforced 100%
coverage gate (`pyproject.toml`'s `[tool.coverage.report].include`, needs
real Firefox not available in CI) — these tests were still added because
untested branching logic is bad practice regardless of what the gate
technically requires, matching this repo's own established standard
(round 35 closed `browser/pool.py`'s coverage gap for the same reason).

## Decision: DLQ Auto-Retry Eligibility Must Follow the Tier-2 Fallback, Not Raw Tier-3 Health

**Date:** 2026-08-14 | **Round:** 37

**What:** `proxy/dlq_reaper.py::_is_eligible()`'s `PROXY_EXHAUSTED` branch
now checks tier 2's `pool_health.py` state instead of tier 3's, when the
DLQ entry's `level_attempted == 3` and
`config.proxy_tiers.allow_tier2_fallback_for_tier3` is enabled.

**Why:** every URL that exhausts all 3 escalation levels DLQs as
`FailureCategory.PROXY_EXHAUSTED` (`orchestrator/worker.py::process_job`'s
`else` clause on the level loop) with `level_attempted = LEVELS[-1] = 3`,
regardless of which specific per-level failure actually caused it.
`PROXY_EXHAUSTED` is a `TRANSIENT_FAILURE_CATEGORIES` member — round 34
built `dlq_reaper.py` specifically so these entries auto-retry once the
relevant tier recovers, no human needed. But the eligibility check used
tier 3's *raw* pool_health state (proxies scoring ≥ 90), and round 33's
own documented finding is that free proxy sources structurally cannot
reach that threshold — confirmed live this round: tier 3 read `CRITICAL`
for the entire session (`l3_ok = 0` throughout), while real level-3 leases
succeeded ~87% of the time via the tier-2 fallback round 33 built for
exactly this reason. The result: every level-3-exhaustion DLQ entry was
**permanently ineligible** for auto-retry — the safety net round 34 built
was silently dead for the exact deployment shape (free-proxy-only,
fallback enabled) this repo runs in, discovered only because this round
cross-referenced round 33's config flag against round 34's eligibility
check with live evidence, which nothing had done before.

**Alternatives considered:**
- **Make `pool_health.py` itself fallback-aware** (classify tier 3 using
  the tier-2 threshold when the fallback flag is on, at the source). 
  Rejected — `pool_health.py`'s tier-3 count still has real diagnostic
  value as-is (an operator legitimately wants to know "how many
  genuinely-90+ proxies exist," e.g. to decide whether to buy paid tier-3
  proxies and flip the fallback flag back off per its own comment in
  `config/base.yaml`). Muddying that signal to serve one caller
  (`dlq_reaper.py`) would make the raw metric less useful for its primary
  purpose. Checking tier 2 at the *point of use* (the reaper, which
  specifically needs to know "will a retry succeed," a different question
  than "how deep is the raw tier-3 pool") keeps both signals honest.
- **Leave it and rely on the round-37 preflight/retry layers alone.**
  Rejected — those layers reduce how often a URL exhausts all 3 levels in
  the first place, but don't help the residual cases at all once an entry
  *is* DLQ'd; without this fix, a DLQ'd URL sat there forever regardless of
  how healthy the pool became, defeating round 34's whole purpose for
  exactly this repo's real deployment configuration.

**Status:** Active. Live-verified directly against the real deployed
config/Redis: a level-3-exhausted `PROXY_EXHAUSTED` entry's eligibility
flipped from `False` (old code, tier 3 reads `CRITICAL`) to `True` (new
code, tier 2 reads `HEALTHY`) with no other change. 4 unit tests
(healthy/degraded unchanged-behavior cases, fallback-enabled level-3
checks tier 2, fallback-disabled level-3 checks tier 3, level-2 entries
unaffected by the flag regardless of its value).

## Decision: Two More Bugs in the Same DLQ Auto-Retry Chain — Status Guard and a UUID Type Mismatch

**Date:** 2026-08-14 | **Round:** 37

**What:** Two more fixes alongside the eligibility fix above, found by
live-verifying it against real production data instead of stopping once
the eligibility check itself looked correct: (1)
`proxy/dlq_reaper.py::_retry_entry()`'s re-enqueue guard broadened from
`WHERE status IN ('FAILED', 'DEAD_LETTER')` to
`WHERE status NOT IN ('PENDING', 'PROCESSING', 'CANCELLED')`; (2)
`storage/dlq.py::DeadLetterQueue._to_entries()` now casts
`job_id=str(r["job_id"])` instead of passing the raw asyncpg row value
through.

**Why:** (1) `worker.py`'s job-status computation never actually produces
`'DEAD_LETTER'` as a job-level status (only appears in a docstring
diagram) and a partial-failure batch (most URLs succeed, a few don't)
settles at `'COMPLETED'`, not `'FAILED'` — so the old guard matched zero
rows for the majority real-world case, live-confirmed against the user's
own actual DLQ'd URLs sitting at `auto_retry_count=0`. (2) `job_id` is a
Postgres `uuid` column; asyncpg returns a native `UUID` object for it, not
a `str`, despite `DeadLetterEntry.job_id` being typed `str` — `rq`'s
`validate_job_id()` rejects anything that isn't a plain string, so
`queue.enqueue(job_id=entry.job_id, ...)` raised `TypeError` on every real
attempt, caught live in the running `dlq-reaper` daemon's own logs the
moment fixing eligibility + the status guard let this line finally get
reached for the first time. **Together with the eligibility fix, this
means round 34's DLQ auto-retry mechanism had never once successfully
re-enqueued a job in this repo's entire history** — three independent
bugs each silently masked the others, so none was individually visible
without fixing the rest first.

**Alternatives considered:** none meaningfully distinct — both are
straightforward correctness bugs (a stale status-string guard not
matching the state machine's real terminal states; a missing type
coercion at a data-access boundary) with one obviously correct fix each,
not judgment calls between competing designs.

**Status:** Active. Live-verified end-to-end against real historical
production data (not synthetic test entries) — the real `dlq-reaper`
daemon's next two natural 60s cycles logged `retried=20` twice (40 total)
against the user's own original batch-test DLQ entries; confirmed
specific previously-dead URLs (`investdelta.ng`, `dida.deltastate.gov.ng`)
now return real `200 OK` on retry. New tests:
`test_retry_guard_includes_completed_not_just_failed` (asserts the actual
SQL string, since a mocked `pg.fetchrow` can't itself catch a
syntactically-fine-but-semantically-wrong WHERE clause) and
`test_list_for_tenant_casts_non_str_job_id` (uses a stand-in object with
its own `__str__`, since a plain string input can't distinguish "cast
happened" from "cast was a no-op").

## Decision: Harvest Source Breadth Over Per-Cycle Speed — Every Source Tried Every Cycle

**Context:** Round 37 closed the L2/L3 proxy-leasing code path; the
remaining open item was proxy *supply* — free-source L2/L3-caliber counts
are thin and volatile. User was asked directly (more free harvest sources
vs. a paid proxy tier) and chose more free sources. While adding 4 new
sources to `proxy/harvester.py`'s `SOURCES` tuple, found that
`_direct_scrape`'s `if total >= limit: break` had been silently starving
sources ordered late in the tuple — once an early source alone filled the
per-cycle `limit` (default 100), the loop stopped and later sources never
ran, confirmed via 2+ days of zero Redis source-health records for
`pubproxy`/`proxyscrape_getproxies`.

**Decision:** Removed the early break. Replaced it with a
`MIN_PER_SOURCE = 10` floor — each source's call gets
`max(limit - total, MIN_PER_SOURCE)`, so a source late in the tuple always
gets a real shot even if earlier sources already met the nominal budget.

**Why:** The goal of adding sources at all is breadth (diversify supply
so no single source's outage or a shifted budget starves the rest), not
just raw per-cycle volume. A volume-only fix (e.g. just raising `limit`)
would still let one prolific-but-low-quality source crowd out the others
whenever it responds fast. Guaranteeing a floor per source directly
targets the actual failure mode that was found.

**Trade-off accepted:** a harvest cycle can now run longer than the
configured `interval_seconds` (600s) when many sources are slow to
respond or mostly return dead proxies, since every cycle now attempts all
12 sources instead of stopping early. Confirmed this is safe, not a
latent bug: `core/periodic.py::run_periodic` `await`s each cycle to
completion and only calls `asyncio.sleep(interval_seconds)` afterward —
cycles are strictly sequential, never overlapping. A slow cycle delays
when the next one starts; it cannot corrupt state or run two harvests
concurrently.

**Alternatives considered:** raising `limit` alone (rejected — doesn't
fix the actual starvation mechanism, just delays when it recurs as more
sources are added); running sources concurrently via `asyncio.gather`
instead of sequentially (rejected for this round — bigger change to
`_direct_scrape`'s shape than the bug warranted; the sequential
awaited-per-source form is also what keeps the safety argument above
simple. Worth reconsidering if cycle duration becomes an actual operational
problem, not just longer than before).

**Status:** Active. Live-verified post-deploy (not just unit-tested) —
rebuilt `api`+`worker-l1/l2/l3` together, first harvest cycle's log line
showed all 12 sources contributing, including the two previously-starved
ones (`pubproxy=2`, `proxyscrape_getproxies=5`) and the 3 newly-added
sources (`shiftytr_http=4`, `clarketm_github=2`, `sunny9577_github=10`).
Full detail and exact before/after `proxy_pool` counts:
`technical-debt.md`'s round-38 entry.

## Decision: Measure Judge Validation Latency Around Only the Winning Request

**Context:** Same round-38 investigation. `proxy/harvester.py::_http_validate`
tries up to 3 `JUDGE_URLS` in sequence, stopping at the first that answers.
Every caller (`_scrape_one`, `_harvest_via_broker`, `promote_tcp_only`,
`promotion.py::_try_one`) measured `latency_ms` by wrapping
`time.monotonic()` around the *entire* `_http_validate()` call. When an
earlier judge candidate was slow or unreachable, its full `timeout` (5.0s)
got silently counted as part of the proxy's own latency before a later
candidate ever answered. Confirmed live: real pool `response_time_ms`
values clustered just above multiples of 5000ms (6805ms, 11878ms, etc.),
consistent with 1-2 dead judge attempts eating their timeout before a
working one answered in an ordinary ~1-2s. Since `latency_score` carries
the heaviest single weight in the scoring formula (up to 45% for a
first-ever validation), this was silently capping most of the pool well
below the L2 (70) threshold and made L3 (90) arithmetically unreachable
for nearly any proxy regardless of true quality — see the L3 root-cause
entry below and `technical-debt.md`'s round-38 entry for the full math.

**Decision:** `_http_validate` now times each judge attempt individually
and returns `(is_valid, anonymity, latency_ms)` — `latency_ms` reflects
only the request that actually succeeded, `None` when none did. All 4
call sites updated to use the returned value directly instead of wrapping
their own timer.

**Why:** The proxy's real network latency is what the scoring formula is
trying to measure; time spent waiting out an unrelated candidate's
failure is noise that has nothing to do with the proxy's own performance.
Fixing the measurement at its source (inside `_http_validate`) fixes it
for every caller at once, rather than patching each call site's timer
logic independently and risking drift.

**Alternatives considered:** shortening `HTTP_VALIDATE_TIMEOUT`/reducing
`JUDGE_URLS` to fewer candidates (rejected — reduces worst-case latency
pollution but doesn't eliminate it, and the multi-judge fallback exists
specifically because a single public judge can go down, per round 32's
own finding; removing candidates trades one known failure mode for
another). Per-candidate individual timing (chosen) directly fixes the
actual defect instead of working around it.

**Status:** Active. Unit-tested (`test_latency_measures_only_the_winning_
attempt` — a slow-then-fast fake client proves the returned latency
excludes the first candidate's delay; `test_latency_is_none_when_invalid`).
Live-verified: pool `response_time_ms` values dropped from the 6000-12000ms
range to realistic 100-2100ms for the same class of real proxies
immediately after deploy; produced this pool's first-ever L3-caliber
(score ≥90) proxy on the very next validation.

## Decision: Refresh Proxy Latency On Every Health Check Instead Of Freezing It At Harvest Time

**Context:** Same round-38 investigation, continued. Fixing the latency-
measurement bug above (previous decision) only helps *newly* validated
proxies going forward — `ProxyManager.mark_success`/`mark_failure`
(`proxy/manager.py`) recompute `reliability_score` on every real fetch
outcome, but always read the STORED `response_time_ms` off the row; they
have no fetch-time latency input of their own. A proxy's latency reading
was therefore captured exactly once, at harvest or promotion time, and
never touched again for the rest of its life — even a proxy that went on
to earn a flawless real-world success rate stayed capped by whatever
single sample (good or, before the fix above, frequently bad) it happened
to get on day one. Worked through the math: even 100% success + elite
anonymity + residential ASN + fresh recency caps at ~83 total if
`response_time_ms` is stuck at the old-buggy 6805ms sample — still under
the 90 L3 threshold. This is why L3 stayed at 0 even immediately after
the first fix above landed a single 96.85-scored proxy: that one instance
got lucky by being freshly harvested post-fix; the rest of the pool's
elite/residential proxies were still carrying their original bad samples.

**Decision:** `health_monitor.py`'s existing `check_all()` cycle (already
re-validates every pooled proxy on a rolling oldest-`last_validated`-first
basis, every `health_interval_seconds` — default 300s, so it eventually
covers the whole pool) now performs a real rescore on a passing
validation instead of only bumping `last_validated`. `check_one()` was
rewritten to delegate to `ProxyHarvester._http_validate` (was a
hand-rolled duplicate of the same JUDGE_URLS loop, discarding everything
but a bool) plus a new ASN classify() call, giving a fresh
`(anonymity, asn, latency_ms)` triple every cycle. On success,
`anonymity_level`/`asn_class`/`response_time_ms` are UPDATEd and
`reliability_score` is recomputed via `ScoringEngine`, folding in the
proxy's real `global_success_count`/`global_failure_count` so accumulated
track record isn't discarded by this cycle — only the latency/anonymity/
ASN inputs get refreshed, not the usage history.

**Why:** The actual root cause wasn't that free proxies can't reach L3 —
it's that nothing ever gave an already-harvested proxy a second chance at
an accurate measurement. A health-check cycle that revalidates every
proxy anyway was already the natural place to also refresh the inputs
that feed its score, rather than adding a separate new mechanism.

**Found and fixed a real regression from this same change before calling
it done:** adding a DNS `classify()` call per successful validation, on
top of the existing per-proxy judge round-trip, made a fully-sequential
100-row cycle balloon to 15-25+ minutes wall-clock — confirmed live, a
fresh deploy's first cycle hadn't logged completion after 10+ minutes
while `last_validated` timestamps were still visibly advancing row by
row. Fixed by bounding concurrency to `HEALTH_CHECK_CONCURRENCY = 5`
(`asyncio.Semaphore`), mirroring `promotion.py`'s existing
`PROMOTION_CONCURRENCY` pattern exactly rather than inventing a new
concurrency-control convention. Also consolidated the pre-existing
per-row `DELETE FROM proxy_pool WHERE reliability_score <= 0` (redundant
— every iteration deleted every currently-zero-score row, not just ones
this iteration caused) into a single pass after the batch.

**Alternatives considered:** threading real fetch `duration_ms` (from
`FetchResult`, available at `mark_success`/`mark_failure`'s call site in
`orchestrator/worker.py`) into `response_time_ms` instead (rejected —
`duration_ms` for L2/L3 includes browser launch, navigation, and
challenge-solving/polling time, not raw proxy connect latency; reusing it
would reintroduce the exact same class of measurement-contamination bug
just fixed above, via a different path). A dedicated new periodic
re-validation job instead of extending `health_monitor.py` (rejected —
`check_all()` already does the right rolling-coverage validation pass;
adding a second parallel mechanism would duplicate it for no benefit).

**Status:** Active. Unit-tested (`test_check_all_rescoring_uses_fresh_
reading`, `test_check_one_delegates_to_http_validate`,
`test_check_one_uses_injected_classifier`,
`test_check_all_bounds_concurrency` — the last asserts
`HEALTH_CHECK_CONCURRENCY` is actually respected under a slow mocked
`check_one`, not just that a semaphore object exists somewhere). Full
suite: 818 passed, 100.00% coverage, ruff/mypy --strict clean.
Live-verified end-to-end: rebuilt/redeployed twice (once per fix above);
after the concurrency fix, the first health cycle completed in a few
minutes (`periodic_health_cycle: {'validated': 21, 'removed': 16,
'downgraded': 79}`) and `proxy_pool` moved from single-digit L2-caliber /
0 L3-caliber to **43 L2-caliber (≥70) and 5 L3-caliber (≥90)** after
touching only 100 of 1,678 pooled rows — one cycle, ~6% of the pool. All
5 L3 proxies real, live, elite anonymity + residential ASN + sub-2.1s
response times.

## Decision: Raise lease_preflight's Timeout From 2.0s to 4.0s

**Context:** Same round-38 investigation, third layer. A real downstream
consumer (research_agent tenant) reported the fix "didn't help" — their
corpus stayed stuck, with `circuit_open` now appearing heavily. Confirmed
via live Redis evidence this tenant was genuinely hitting this exact
deployment. Root-caused to two things: (1) stale circuit-breaker state
from failures predating this round's fixes — not a code bug, resolved by
manually clearing the affected `cb:*` keys at the user's request; (2) the
real bug — `net_probe.py::lease_preflight`'s fixed 2.0s timeout (round
37's own original choice) was rejecting proxies with a real,
accurately-measured (per this round's earlier `_http_validate` fix)
judge-latency of 1.9-3.2s. Watched a freshly-recovered pool (43 L2-caliber
/ 5 L3-caliber) collapse back to 0/0 within ~75 minutes of real traffic —
direct evidence on the 5 original L3 proxies showed 0 successes, 3-11
failures each, judge-latencies of 607-3242ms, several exceeding the 2.0s
budget outright. `mark_failure` was correctly doing its job, but the thing
it was punishing was a too-tight clock, not real unreliability — creating
a tight feedback loop that erased the earlier fix's gains under real load.

**Decision:** Raised `http_probe`/`lease_preflight`'s default timeout
2.0s → 4.0s. Left `tcp_probe`'s own separate default unchanged (used
directly by `harvester.py`'s unrelated candidate pre-filter, not
implicated in this failure).

**Why:** The scoring fix earlier this round made genuinely-1-3s-latency
proxies visible to the system for the first time (previously their
latency was over-measured as much worse, so they never scored high enough
to reach this code path at all). The lease-time preflight's fixed budget
was calibrated against the OLD, artificially-slow-looking pool and never
revisited once the measurement got fixed. Fixing measurement without also
revisiting downstream fixed timeouts that assumed the old (wrong)
distribution left a real gap.

**Trade-off, asked the user rather than deciding unilaterally:** three
options — raise the timeout (chosen), scale it per-proxy off its own
known `response_time_ms` (more precise, more code), or leave it as a
deliberate fast-proxies-only filter. Raising it was the simplest fix that
directly addresses the confirmed mechanism. Worst-case proxy-exhaustion
path across `MAX_ATTEMPTS=5` grows from 20s to 40s — accepted, since it's
still well under round 37's original problem (a single bad lease costing
a full 40-60s browser navigation timeout with zero preflight at all).

**Alternatives considered:** per-proxy adaptive timeout (rejected for this
round — real fix, but bigger change; worth reconsidering if a flat 4.0s
still proves too tight or too loose once more data comes in over more
health cycles).

**Status:** Active, live-verified but still stabilizing at time of
writing — rebuilt/redeployed, pool showed its first post-fix L3 proxy
(score 98.66) within ~2 minutes and L2 count climbing from 0. Success
rate over the following ~15 minutes of real tenant traffic: ~55-67% (up
from ~20-30% during the collapse), `circuit_open` at 0 in every bucket
since the manual reset. Full detail: `technical-debt.md`'s round-38
entry, third sub-section.

## Decision: A Failed Health Check Must Also Refresh last_validated

**Context:** Same round-38 investigation, fourth layer. User pushed back
a second time on "free proxies are just unreliable" as an explanation for
why the pool wasn't fully recovering even ~1.5 hours after the timeout
fix, asking to dig deeper rather than accept it. That skepticism was
right. The `health_monitor.py::check_all` downgrade count had been
suspiciously constant (65-80) every single cycle for hours — too
consistent for genuine pool-wide churn — and a direct query confirmed 61%
of the pool (833/1,361 rows) hadn't been re-checked in over an hour
despite the cycle running every 5-8 minutes that whole time.

**Root cause (pre-existing, confirmed via `git blame` to predate round
38):** the query always selects the oldest-`last_validated` 100 rows each
cycle, but only the SUCCESS branch ever updated that timestamp. A proxy
that failed once kept its old timestamp forever, so it stayed permanently
at the front of the "oldest" ordering — re-picked and re-punished every
cycle, forever, while the rest of the pool never got reached.

**Decision:** The downgrade branch now also sets `last_validated = NOW()`,
matching the success branch.

**Why:** A rolling-coverage design (`ORDER BY <staleness> LIMIT N` every
cycle) fundamentally requires "when did we last look at this" to advance
regardless of outcome — otherwise a failure permanently glues its own row
to the front of the queue and the design's core assumption (the cycle
eventually reaches the whole pool) silently breaks.

**Alternatives considered:** none meaningfully distinct — this is a
straightforward correctness bug (the two branches diverged when they
should track the same "attempted" signal), not a judgment call between
designs.

**Status:** Active. Unit-tested
(`test_check_all_downgrade_refreshes_last_validated`). Full suite: 819
passed, 100.00% coverage. Live-verified across two full cycles:
60-min-stale count 833 → 712 → 773 (time alone, between cycles) → 670 —
net downward trend confirms the queue genuinely advances now instead of
reprocessing the same stuck batch.

---

## Decision: Score From Real Track Record, Not Stale Snapshots

**Date:** round 39

**Context:** User reported a real production run (research_agent tenant,
47/47 URLs, only 10 succeeded) and explicitly rejected "free proxies are
just unreliable" as an explanation, asking for the real mechanism rather
than accepting a plausible-sounding one. That skepticism uncovered three
compounding bugs, each found only by live re-verifying the previous fix
instead of assuming it was sufficient.

**What:** (1) `health_monitor.py::check_all` now does a fresh
`UPDATE ... RETURNING` immediately before scoring/writing each row,
instead of scoring off a batch snapshot read once at the top of the
cycle — the snapshot was stale by write time, silently clobbering real
`mark_success`/`mark_failure` updates that landed mid-cycle. (2)
`harvester.py`'s re-harvest and promotion paths now look up a proxy's real
`global_success_count`/`global_failure_count` and pass the real
`compute_success_rate()` result to `ScoringEngine`, instead of hardcoding
`success_rate=None` (which `scoring.py` deliberately reserves for "no
track record yet," redistributing weight across the other four scoring
dimensions) even for proxies with real accumulated history. (3) Removed
`GREATEST(reliability_score, EXCLUDED.reliability_score)` from both
`ON CONFLICT DO UPDATE` upserts in `harvester.py` — a ratchet that only
ever lets a score increase, originally meant to protect a good score from
a transient bad re-read, but which also permanently protects a WRONG,
inflated score from ever being corrected once the real formula says it
should drop.

**Why:** Fix (2) alone produced no visible pool-wide change — the only
reason that was investigated further, rather than accepted as "the fix
just doesn't matter much," was fix (3): the ratchet was silently
discarding every corrected (lower) score fix (2) computed. Together, these
three are what surfaced the pool's honest, much lower real supply numbers
(tier 2's real supply: a fake ~45 down to a real 4) — this was a
correction of years of silent inflation, not a regression the fixes
caused.

**Trade-offs:** None significant — these are correctness fixes, not
judgment calls between designs. The corrected (lower, honest) tier-2
supply is what directly motivated the same-round
`allow_tier1_fallback_for_tier2` addition (see next entry) — without it,
this fix alone would have made the starvation worse in the short term by
removing years of score inflation that had been (accidentally) keeping
more proxies eligible than their real track record justified.

**Status:** Active. 835 passed (progression 820→835 across the round),
100% coverage, ruff/mypy clean. Live-verified via real `POST /v1/scrape`
jobs against the `research_agent` tenant. Full detail:
`technical-debt.md`'s round-39 entry.

---

## Decision: Round 39 Leasing-Reliability Hardening

**Date:** round 39

**Context:** Same investigation as the prior entry — once real (lower)
supply numbers were visible, the user asked to understand exactly how the
scraper uses proxies (single lease per site, discarded after?) rather than
accept another surface-level fix, and to pull any thread found rather than
defer it. Four further gaps surfaced this way, each found by live-testing
the previous fix before declaring it sufficient.

**What:**
1. Added `allow_tier1_fallback_for_tier2` to `ProxyTierConfig`, mirroring
   the existing `allow_tier2_fallback_for_tier3` pattern exactly (same
   single-hop-only shape, tried only after a real tier-2-caliber search
   comes up empty, never cascades further).
2. Raised `ProxyManager.MAX_ATTEMPTS` from 5 to 10.
3. `_select_candidate`'s `ORDER BY` now leads with
   `(global_success_count > 0) DESC` before `reliability_score DESC`.
4. `mark_failure` gained `ban_domain: bool = True`, set `False` only at the
   lease-time preflight call site inside `ProxyManager.get_proxy()`'s own
   loop.
5. `net_probe.py::_LEASE_CHECK_URLS` grew from one HTTPS judge to three
   (first-success-wins), mirroring `harvester.py`'s own multi-judge
   pattern.

**Why:** (1)+(2) directly target the corrected-lower supply from the prior
decision — a preflight-bounded attempt costs low single-digit seconds
against a 600s job timeout, so doubling the attempt budget is cheap
relative to meaningfully raising the odds of finding one real working
proxy in a low-hit-rate pool. (3): live-measured that a same-moment
liveness probe of the real top-20-by-score candidates found 0/20 alive —
every one had zero track record and a score built purely from a single
judge round-trip (which `scoring.py` deliberately doesn't penalize, since
"no data yet" must not look like "bad"), while a broader same-moment
sample found proven-but-lower-scored proxies alive with no correlation to
score. A proxy that has actually forwarded real traffic before, even
imperfectly, is a better lease-time bet than one that only looked good on
one judge round-trip and has never been used. (4): preflight checks a
third-party judge (`_LEASE_CHECK_URLS`), never the real target domain — a
proxy failing it says nothing about that specific domain, so a full
1-hour domain-specific ban on that basis was locking a proxy out of
exactly the domain it happened to be tried against when momentarily down,
even after it recovered (free proxies churn back alive within minutes,
live-measured this round), while every other domain remained free to try
it immediately. (5): the same class of bug the multi-judge fix in
`harvester.py` already guards against — one flaky/rate-limited judge
previously looked identical to a dead proxy, false-negativing a genuinely
working proxy straight into `mark_failure`.

**Trade-offs:** (2) raises the worst-case exhaustion path's latency
(bounded, still well under the 600s job timeout). (5) raises
`lease_preflight`'s worst case from `2×timeout` to `(1+3)×timeout` per
candidate (only hit if TCP connects but all three judges simultaneously
time out for that specific proxy) — accepted because the common cases
(proxy dead at TCP, or the first judge answers) are unaffected, and a
correctly-scored-but-moderately-slow proxy getting a fair chance matters
more than shaving the theoretical worst case.

**Alternatives considered:** For (3), a fully independent adaptive
ordering formula was considered and rejected as unnecessary complexity —
leading with a boolean "has real history" split before falling back to
the existing score ordering was the smallest change that fixed the
measured problem.

**Status:** Active. All four fixes verified live in the same
`research_agent` batch re-run: zero domain-ban keys created after a full
run (proving fix 4), both `proxy_tier2_fallback_to_tier1` and
`proxy_tier3_fallback_to_tier2` firing correctly in worker logs. Full
detail: `technical-debt.md`'s round-39 entry.

---

## Decision: Toggleable Paid Proxy Gateway

**Date:** round 40

**Context:** Following round 39's fixes, the user asked directly: if they
provide a paid residential proxy (DataImpulse — rotating, HTTP/HTTPS,
gateway host/port + username/password auth), would that guarantee solving
the scrape-failure issue? The honest answer given was no guarantee, but
that it would directly target the specific, measured bottleneck from
rounds 38-39 (only 23/868 free-harvested proxies ever recorded a real
success; ~16% live-measured liveness) — real per-target detection/blocking
is a separate, unproven variable. The user chose to proceed, with an
explicit, non-negotiable requirement: additive and toggleable, never a
replacement for the free-pool system.

**What:** Three-way `config.dataimpulse.strategy` (`free_only` default /
`paid_only` / `free_first`), implemented as a branch inside
`Worker._fetch_with_proxy()` rather than inside `ProxyManager.get_proxy()`
— `ProxyManager` stays entirely `proxy_pool`-table-scoped, single
responsibility. The gateway itself is a synthetic `Proxy` object
(`proxy/paid_gateway.py::build_gateway_proxy()`, pure function, 4 env
vars, no network I/O, no DB row) constructed fresh per lease attempt, not
a permanent high-score `proxy_pool` row.

**Why (bypass the DB pool entirely, don't force the gateway through
it):** A rotating gateway has no fixed identity worth scoring or banning —
the exit IP changes server-side per connection, so (1) `mark_success`/
`mark_failure` scoring the gateway's own static `ip:port` would be scoring
the wrong thing (not what actually succeeded or failed), (2) a
domain-specific ban on that static `ip:port` would incorrectly lock out
every future *different* real exit IP behind it, and (3) `lease_preflight`
(TCP+HTTPS against the gateway host:port) would almost always pass since
the gateway itself is always up — it doesn't test the thing that could
actually fail. Modeling it as a `proxy_pool` row (the alternative
considered) would have been simpler to wire but conflated a "scored pool
of individually-tracked IPs" abstraction with a "always-available rotating
gateway" that doesn't fit that shape.

**Why fail-fast at `Worker.__init__`, not silent fallback:** if
`dataimpulse.enabled=true` but the 4 required env vars aren't all set,
`Worker.__init__` raises `RuntimeError` immediately rather than having
`_fetch_with_proxy` silently degrade to the free pool. RQ forks one worker
process per job, so this fails only the job(s) that process would have
handled, loudly, at the earliest possible point — a misconfigured toggle
should be impossible to miss, not silently indistinguishable from
`free_only` behavior.

**Trade-offs:** Two real Docker-image gaps only surfaced once the gateway
path was actually exercised for the first time — Botasaurus's
credentialed-proxy handling needs both `nodejs` and `npm` on the image
(neither was there; no proxy before round 40 ever carried credentials, so
this code path was structurally unreachable until now). Both fixed in the
same round (see `technical-debt.md`'s round-40 entry) — accepted as the
cost of exercising a genuinely new code path for the first time, not a
design flaw in the toggle itself.

**Alternatives considered:** Env-var-controlled toggle (rejected —
user explicitly chose `config/base.yaml`, matching every existing
proxy-tier toggle in this repo, e.g. `allow_tier2_fallback_for_tier3`).
Modeling the gateway as a `proxy_pool` row (rejected, see above).

**Status:** Active, but shipped with `dataimpulse.enabled: false` (safe
default — zero behavior change unless explicitly opted in). The
Camoufox+gateway path is live-proven working end to end (direct isolated
test: real 200, 244,829 bytes of real page content through the gateway).
The full L1→L2→L3 job pipeline under `paid_only`/`free_first` is NOT yet
called fully reliable — see `technical-debt.md`'s open Xvfb-collision
thread. 858 passed, 100.00% coverage (verified in a clean shell with no
env vars set, matching CI), ruff/mypy clean. Full detail:
`technical-debt.md`'s round-40 entry.

---

## Decision: New Paginated Routes Use Plain `int` Params, Not FastAPI's `Query(...)`

**Date:** 2026-08-17 | **Round:** 56

**What:** `api/routes.py`'s new `GET /v1/jobs` and `GET /v1/dlq` (and any
future paginated route in this file) validate `limit`/`offset` with a
shared `_validate_pagination(limit: int, offset: int) -> None` helper
(next to the existing `_validate_uuid()`), raising `HTTPException(422,
...)` manually — not FastAPI's `Query(default, ge=..., le=...)` marker.

**Why:** `Query(50, ge=1, le=500)` as a Python default value is only ever
resolved to its plain `50` by FastAPI's dependency-injection layer when
the endpoint runs through a real ASGI request. Every route function in
`api/routes.py` is also called directly from unit tests
(`tests/unit/test_api_routes.py`) — `await list_jobs(x_api_key="sk-admin")`
— which bypasses that DI layer entirely, so `limit` would be the literal
`Query(50)` sentinel object, not `50`. First test run of `GET /v1/jobs`
failed with `assert Query(50) == 50`. This is the same class of gotcha
already noted on `ScrapeRequest.idempotency_key`'s `Header()` default
(round 29's comment in the same test file) — a project-wide pattern now,
not a one-off.

**Tradeoffs:** Loses FastAPI's automatic OpenAPI-doc generation for the
`ge`/`le` constraint (it'd show up in `/docs` for free with `Query()`).
Gains: routes stay callable and testable as plain async functions without
standing up a full ASGI test client for every unit test, matching every
other route in this file (none of which use FastAPI's `Query`/`Body`
markers either — this decision keeps that consistent rather than
introducing the one exception).

**Alternatives considered:** Use `Query()` and switch all direct-call unit
tests to go through `fastapi.testclient.TestClient` instead (rejected —
would mean rewriting the entire existing `test_api_routes.py` suite's
calling convention for one new route, far more invasive than the problem
warrants). Resolve `Query()` defaults manually inside the route body via
`if isinstance(limit, Query): limit = 50` (rejected — fragile, couples
route logic to FastAPI's internal marker type instead of just not using
the marker).

**Status:** Active. Applies to `list_jobs()` and `list_dlq()`
(`api/routes.py`); should be followed by any future paginated route added
to this file. Full detail: `technical-debt.md`'s round-56 entry.

---

## Decision: Botasaurus Navigation Failure Reuses `BROWSER_CRASH`, No New `FailureCategory`

**Date:** 2026-08-17 | **Round:** 57

**What:** The fix for Botasaurus/Chromium silently returning its own
internal network-error interstitial as `success=True` content
(`browser/_botasaurus_nav_check.py::raise_if_navigation_failed()`) raises
a new `BotasaurusNavigationError` — a plain `Exception` subclass — rather
than introducing a new `FailureCategory` enum value. It flows through
`classify_fetch_exception(exc, FailureCategory.BROWSER_CRASH)`, the exact
default `level_2.py`/`level_3.py`'s outer exception handlers already use
for any browser-level operational failure.

**Why:** `BROWSER_CRASH` is already semantically correct — the browser
genuinely failed to load the real page — and already has fully correct,
battle-tested handling: `_PROXY_ATTRIBUTABLE_CATEGORIES` (round 37) treats
it as proxy-attributable and triggers a same-level fresh-proxy retry
before escalating; the circuit breaker, DLQ eligibility, and
`proxy/dlq_reaper.py`'s auto-retry all already classify it correctly as
transient/retryable. A new category would have meant touching
`DLQ_ELIGIBLE_CATEGORIES`, `TRANSIENT_FAILURE_CATEGORIES`,
`dlq_reaper.py`'s own separate category list, and possibly a migration —
real blast radius across already-working, already-tested machinery for a
failure mode that behaves identically to an existing one in every way
that matters (proxy-attributable, retryable, escalates on repeat
failure). The distinguishing detail the user asked for ("say exactly what
it is") lives in `error_message` instead — `BotasaurusNavigationError`'s
message names the specific `chrome-error://` state — which every existing
consumer of that field (DLQ entries, `GET /v1/jobs/{id}/dlq`, round 56's
`GET /v1/dlq`, structured logs) already surfaces, no new plumbing needed.

**Trade-offs:** A DLQ entry or dashboard filtering strictly on
`failure_category=browser_crash` cannot distinguish "Botasaurus hit
Chromium's own error page" from any other browser-crash-shaped failure
(e.g. a real Xvfb/display crash) without also reading `error_message`.
Accepted — the category taxonomy's job is retry/circuit-breaker/DLQ
*behavior*, which is genuinely identical for both; the human/diagnostic
distinction belongs in the message text, not a proliferation of
categories that would otherwise need its own retry-eligibility rule
threaded through every category-keyed list in the codebase.

**Alternatives considered:** A new `FailureCategory.BROWSER_NAVIGATION_ERROR`
(rejected — see blast-radius reasoning above; also would need its own
correct placement in `_PROXY_ATTRIBUTABLE_CATEGORIES`/
`TRANSIENT_FAILURE_CATEGORIES`/`dlq_reaper.py`'s list to behave right,
duplicating `BROWSER_CRASH`'s existing correct behavior for no semantic
gain). Detecting the failure via the rendered interstitial's text instead
of `driver.current_url` (rejected as the *primary* mechanism, kept as
defense-in-depth in `ChallengeDetector` — Chromium's error-page heading/
body text is locale-dependent; `current_url`'s `chrome-error://` scheme is
not, and covers the whole `net::ERR_*` failure class with one check
instead of enumerating wordings).

**Status:** Active. Full detail: `technical-debt.md`'s round-57 entry.

---

## Decision: Knowledge-Audit — Round-57 CLAUDE.md Diary Regression

**Date:** 2026-08-17 | **Round:** 57 (knowledge audit, user-invoked via `/knowledge-audit`)

**What:** `CLAUDE.md`'s opening paragraph had regrown into a ~4000-word,
round-by-round dated narrative (every round 37 through 57, each with its
own "Round N (..." block) — the exact same failure mode a round-28
knowledge-architecture audit had already fixed once, documented in
`CLAUDE.md`'s own "Evolution history" bullet as "was 7 growing paragraphs
here." Trimmed back to a one-line "Currently at round 57" pointer, and the
existing terse "Evolution history" bullet (which already compactly covered
rounds 22-38 in ~280 words) was extended in the same one-clause-per-round
style to cover rounds 39-57 too, so the compact orientation this file is
supposed to provide stays current without re-accumulating prose. Also
found and fixed the same drift pattern in `.claude/MEMORY.md`'s "Current
state, for a quick orientation" section — it had frozen at round 40 while
the project moved through round 57 (self-labeled "Since then (rounds
35-40, not yet folded into the paragraph above)" with nothing ever folded
in after); rather than re-fix it as a second rolling summary (which just
recreates the two-copies-that-drift-apart problem), it now points at
`CLAUDE.md`'s Evolution History bullet as the one place that summary
lives.

**Why:** `CLAUDE.md` is loaded into every session's context regardless of
task relevance — this project's own `knowledge-audit`/`knowledge-
maintainer` skills state it should be "small, stable, high-signal —
navigation and architecture only, never a project diary." A ~4000-word
diary paragraph that grows every round is the opposite of that, and
because every round's content already had a full, real home in
`technical-debt.md` (verified round-by-round before trimming — every round
37-57 cited in the old paragraph has either its own `## ... (as of round
N)` section or, for round 47, a `RESOLVED (round 47)` bullet nested inside
round 48's section), nothing was lost by removing the diary copy — it was
pure duplication of content that already existed in more permanent,
better-organized form.

**Trade-offs:** The one-clause-per-round "Evolution history" bullet is
necessarily lossy compared to the full paragraphs it replaces — a reader
gets "what happened," not the full root-cause reasoning, live-verification
detail, or numbers. That's intentional: this file's whole job is
navigation, and `technical-debt.md`'s "Full detail: ... round-N entry"
pointer (unchanged) is exactly how a reader gets the rest.

**Alternatives considered:** Leaving `CLAUDE.md`'s diary paragraph in place
and only trimming future rounds going forward (rejected — doesn't fix the
existing ~4000 words of bloat already there, and does nothing to prevent
the same regression happening a third time, since the incentive that
caused it twice — "just prepend this round's summary, matches the existing
pattern" — would still be the path of least resistance for the next
session). Deleting `MEMORY.md`'s stale orientation section outright
instead of pointing it at `CLAUDE.md` (rejected — the section itself, as a
concept, is useful; the fix is one canonical copy, not zero copies).

**Status:** Active. Full detail: `technical-debt.md`'s round-57 entry
(knowledge-audit subsection).

---

## Decision: Ship `BotasaurusConfig.lang` Despite Live Evidence It Doesn't Spoof `navigator.language`

**Date:** 2026-08-17 | **Round:** 60

**What:** Round 60 wired `Driver(lang=...)` (a real `botasaurus_driver`
ctor kwarg) through as `BotasaurusConfig.lang`. Live testing against the
installed `botasaurus_driver==4.0.100` / Playwright Chromium 1228 build
showed it has **zero observed effect** on `navigator.language`,
`navigator.languages`, or the `Accept-Language` request header — despite
`driver.py:2153`'s own docstring explicitly claiming `navigator.language`
"comes from the lang option." Tested both `"de-DE"` and `"de"` formats;
confirmed the Chromium build does ship a matching `de.pak` locale
resource, so it isn't a missing-locale-data explanation. A JS-injection
workaround (`driver.run_on_new_document()`) was attempted and hit a
separate, confirmed-real upstream bug: `driver.run_cdp_command(cdp.page.
enable())` throws `ChromeException("Invalid parameters ... CBOR: map
start expected")` in this installed version — verified not a general
zero-param-command issue (`cdp.dom.enable()`/`cdp.runtime.enable()` both
succeed) — so it's Page-domain-specific breakage inside the installed
package, not something to route around within this task's scope. The
field shipped anyway, config-gated and off by default, with the
limitation documented directly in `config/schema.py`'s docstring.

**Why:** `lang` is still a real, correctly-forwarded Driver kwarg
(confirmed present on the actual Chrome command line via
`chrome://version`) — its ineffectiveness for `navigator.language`
spoofing is a property of *this installed browser build*, not evidence
the kwarg itself is fake or that the wiring is broken. Removing it
entirely would erase a real capability that may behave correctly on a
different Chromium version (Chrome's handling of `--lang` for renderer-
visible bindings has shifted across versions in ways this session didn't
have the surface to fully audit) — better to ship a real, verified,
honestly-documented kwarg than to either (a) silently claim it works when
live evidence says it doesn't, or (b) delete a legitimate capability over
one environment's specific behavior. This is the project's "evidence over
assertion" operating rule cutting against removing something, not just
for adding it.

**Trade-offs:** A caller who sets `botasaurus.lang` expecting
`navigator.language` spoofing (the documented behavior) will not get it
against this stack today — the schema docstring is the only place this is
flagged; there's no runtime warning if the field is set. If this bites a
real caller, the fix is either a runtime log line when `lang` is set, or
finally patching around the `Page.enable()` CDP bug (vendoring a patched
`botasaurus_driver`, a much bigger undertaking, or filing/watching an
upstream fix).

**Alternatives considered:** (1) Drop the `lang` field entirely and only
ship `locale`/`timezone` (which do work) — rejected per the reasoning
above, this throws away a real capability over one build's limitation.
(2) Chase the `Page.enable()` CBOR bug to make the JS-injection workaround
functional — rejected as out of scope for this round: it would mean
patching or vendoring the installed `botasaurus_driver` package, a
materially bigger and separate initiative from "wire the real config
kwargs" that the other 3 items in this round stayed scoped to. (3) Ship
`lang` silently with no docstring caveat, treating "kwarg is real and
forwarded" as sufficient proof of correctness — rejected, this is exactly
the kind of unverified claim the project's evidence-over-assertion rule
exists to prevent; the live test was cheap to run and directly
contradicted the upstream docstring.

**Status:** Active. Full detail: `technical-debt.md`'s round-60 entry.

---

## STATUS.md Stale Zero-Concurrency Claim (Round 49 Fix Never Reflected)

**Date:** 2026-08-18

**Context:** User asked why scrape jobs regularly exceed 120s given the
`job_timeout` formula (`max(600, url_count * 120)`, `api/routes.py:247`).
While answering, `.wolf/STATUS.md`'s "Genuinely open" list (item 4) was
cited as background — it claimed `process_job` still ran URLs strictly
sequentially with zero intra-job concurrency, "deliberately deferred per
explicit user instruction," pointing at the round-45 entry.

**Finding:** That claim was stale. Round 49 (`orchestrator/worker.py:245`,
comment block at line 226) replaced the sequential `for url in
request.urls:` loop with `asyncio.Semaphore`-bounded concurrent dispatch
(`_dispatch_one_url`/`_process_one_url`), default cap
`politeness.max_concurrent_urls_per_job = 5` (`config/base.yaml:168`).
CLAUDE.md's own "Evolution history" bullet already documented this round
49 change correctly — only `.wolf/STATUS.md` had drifted, carrying the
round-45 framing forward 11 rounds past its own fix without ever being
corrected.

**Why it matters:** `STATUS.md` is the explicit single-source-of-truth,
read-first document per `.wolf/OPENWOLF.md`. A stale claim there about a
core execution-model property (sequential vs. concurrent) risks a future
session re-implementing already-shipped concurrency, or mis-explaining
real job-latency behavior to a caller — which is exactly what almost
happened here.

**Fix:** `.wolf/STATUS.md` item 4 corrected in place with the real
current state, the round-49 code references, and a pointer back to this
entry. The real explanation for jobs exceeding 120s is the interaction of
the 5-way concurrency cap with per-URL L1(20s)/L2(40s)/L3(60s) escalation
cost (each level retried once with a fresh proxy on proxy-attributable
failure per round 37) — not sequential processing.

**Status:** Active — `max_concurrent_urls_per_job` raise/lower is a
politeness/anti-detection tradeoff, left as-is unless the user asks.

---

## Knowledge-Audit: CLAUDE.md Diary Regression, 3rd Occurrence

**Date:** 2026-08-18

**Context:** User asked for a full knowledge-audit sweep (via the
`knowledge-audit` skill) after a stale-STATUS.md finding (see "STATUS.md
Stale Zero-Concurrency Claim" above) prompted a closer look at the whole
knowledge system. The audit found `CLAUDE.md`'s Evolution History bullet
and Module Map table had regrown into a full round-by-round diary — the
same failure mode fixed at round 28 and again at round 57 — this time
inside the very structures round 57's fix created to prevent it.

**Finding:** Evolution History (~690 words) and Module Map (~2080 words)
had accumulated per-round mechanism narratives with specific figures
(e.g. "804.7MB" RSS measurement, `LocalExtension` implementation detail,
`chrome-error://` scheme name) instead of round-57's established
one-clause-per-round style. File had reached 31.7KB (~8K tokens),
always loaded every session. Verified via grep that every specific
figure and mechanism detail already existed in `technical-debt.md`
before trimming (per the audit skill's explicit rule: never trim
investigation detail on the assumption alone that a pointer covers it —
confirm first) — `LocalExtension`, `804.7MB`/`0.79GB`, and
`chrome-error` all present there.

**Fix:** Re-trimmed both sections to current-state summaries — Evolution
History back to true one-clause-per-round (round number + a few words,
no mechanism prose), Module Map back to package-level current-state
one-liners with round citations removed entirely. File dropped from
31.7KB to 13.8KB (~1557 words). Full detail for both sections remains
exactly where it already lived: `architecture.md` (design) and
`technical-debt.md` (full round history).

**Why it keeps happening:** Nothing mechanically prevents it — the file
carries an explicit warning comment against exactly this (added round
28, reinforced round 57) and it still regrew, because normal end-of-round
edits add "just one more clause" each time and no single edit looks like
regrowth in isolation.

**Recommendation, not yet implemented:** add a CI or pre-commit check
that fails if `CLAUDE.md` exceeds roughly 1500 words, so regrowth is
caught mechanically at the next offending commit instead of waiting for
the next manual audit. This is the fork audit's top future
recommendation; left as an open suggestion since it's a CI/tooling
change, not a knowledge-doc edit.

**Status:** Active. If this happens a 4th time, the CI gate above should
be treated as no-longer-optional.

---

## CLAUDE.md Size Gate — CI + pre-commit (closes prior recommendation)

**Date:** 2026-08-18

**Context:** Closes the open recommendation from "Knowledge-Audit: CLAUDE.md
Diary Regression, 3rd Occurrence" (above) — a mechanical check so the same
regrowth doesn't need a 4th manual audit to catch.

**What shipped:** `tools/check_claude_md_size.sh` — `wc -w CLAUDE.md`, fails
if over 1800 words (file was 1557 words right after the same-day trim; 1800
gives real headroom for legitimate navigation additions without tolerating
regrowth back toward the pre-trim ~4900-word size). Wired into both:
- CI (`.github/workflows/test.yml`, `lint` job, new "CLAUDE.md size gate"
  step, same grep-gate pattern as the existing `no direct fetcher
  construction`/`force_engine` steps — hard fail, not advisory).
- pre-commit (`.pre-commit-config.yaml`, new local `claude-md-size` hook,
  scoped to `files: ^CLAUDE\.md$` so it only runs when the file itself
  changes).

**Why both, not just one:** pre-commit catches it before the commit even
happens (fast local feedback); CI catches it regardless of whether a given
contributor has pre-commit installed — same reasoning as the existing
`ruff`/`mypy-strict` hooks being duplicated in both places.

**Trade-off:** 1800 is a word count, not a token count — a rough proxy.
Good enough here since English prose tokenizes fairly consistently
(~1.3-1.5 tokens/word); a code-heavy file would need a different metric,
but CLAUDE.md is prose by design.

**Status:** Active, live-verified: script runs clean against the current
1557-word file (`CLAUDE.md: 1557 words (limit: 1800)` / `OK`).

---

## Diagnostic Function Existing ≠ Wired Into the Real Path (Round 61)

**Date:** 2026-08-20

**Context:** Investigating a Slack alert ("DLQ has 610+ entries") plus a
stuck `research_agent`-tenant job, traced to `detection_block` DLQ
entries. Full technical account: `technical-debt.md`'s round-61 entry;
bug log: `.wolf/buglog.json` → `bug-r61-01`.

**What was found:** Round 22 (`technical-debt.md`'s round-21/22 entries;
`.archive/evidence/round-19/20-evidence.md`) already root-caused the
exact failure mode hit again here — a NoCaptchaAI account with balance
but no active subscription plan accepts worker-slot-based captcha tasks
(reCAPTCHA v2, Turnstile, GeeTest, MTCaptcha) and silently never solves
them, sitting at `status: "idle"` forever. Round 22 built the correct
detector for it, `NoCaptchaAIClient.has_active_plan()`, and even a
preflight CLI tool (`tools/validate_captcha_keys.py`) that reports it
honestly. But `has_active_plan()` was wired into exactly that one call
site — the manual CLI — and nowhere near the actual runtime solve path
(`_solve_token`, called by every real `solve_recaptcha_v2`/`solve_
turnstile`/etc.). So for the ~2 months between round 22 and round 61,
every real solve attempt against a plan-less account still burned the
full dead poll (measured at 92.64s per attempt, round 61) — the round-22
fix diagnosed the disease correctly but never actually treated it.

**Why this matters beyond this one bug:** the failure isn't "the check
was wrong" — the check was and is correct. The failure is a category
that's easy to miss in review: a function that looks like it closes an
issue (correct logic, has a docstring citing the round it was built,
even a dedicated preflight tool) can still be dead weight on the path
that actually matters, if nothing calls it from there. `grep`-ing for
"does X exist" answers a different question than "is X called from where
it needs to run."

**Decision:** When closing out a round that adds a diagnostic/guard
function specifically to detect a known failure mode, treat "wired into
every real call site that can hit that failure mode" as part of the
definition of done — not just "the detector exists and a manual tool can
report it." Applies most to anything under `services/`/`fetcher/` that
guards a third-party integration's degraded-but-not-erroring state
(the class of bug where the provider returns HTTP 200 and `errorId: 0`
right up until the timeout).

**Status:** Closed for `has_active_plan()`/`_solve_token` specifically
(round 61). Recorded here as a general lesson — also captured in
`.wolf/cerebrum.md`'s Do-Not-Repeat section for session-local recall.

---

## Scoping: Fix CAPTCHA Fail-Fast Now, Defer `proxy_exhausted` (Round 61)

**Date:** 2026-08-20

**Context:** Round 61's DLQ investigation found two largely independent
root causes behind the 610-entry pile: `detection_block` (180/592, 30%,
traced to the NoCaptchaAI wiring gap above — a real, scoped, high-
confidence code fix) and `proxy_exhausted` (298/592, 50%, traced to
proxy-pool composition — 173 residential + 2 mobile out of 740 proxies —
against a broad set of hardened targets; DataImpulse paid-gateway
fallback confirmed correctly wired, ruling out a quick dead-wiring fix
like the captcha one). Full technical detail: `technical-debt.md`'s
round-61 entry.

**Decision, made by explicit user choice when offered the option to dig
further into `proxy_exhausted` in the same session:** implement the
captcha fix now; treat `proxy_exhausted` as a separate follow-up rather
than bundle both into one round. These two DLQ categories share a
symptom (jobs dying, DLQ growing) but not a root cause or a fix shape:
one is a wiring bug fixable in an afternoon with existing infrastructure,
the other is an open question about whether more proxy budget or a real
domain-tier-aware selection feature is the right lever — a bigger design
decision that deserves its own scoped investigation rather than being
rushed alongside an unrelated fix.

**Status:** Captcha fix shipped (round 61). `proxy_exhausted` is the
explicit next candidate quest — see `.wolf/STATUS.md` → "Next phase" for
what's already known going in.

**Follow-up, same day (2026-08-20):** the premise of this deferral was
wrong. Re-investigating `proxy_exhausted` (triggered by a second identical
Slack alert, not a planned follow-up) found that 292 of the 298 rows
predate the `free_first` gateway fallback shipping (2026-08-15) and zero
exist after it except 6 caused by an unrelated politeness-slot bug, fixed
the same session. There was no proxy-supply/quality question to defer —
the scoping decision above turned out to be moot, not merely postponed.
Recorded as a lesson, not a correction of the decision itself: choosing
to scope work apart was still the right call given what was known at the
time (see the round-22-regression lesson entry above for the general
principle — "diagnostic function existing ≠ wired in" — this is that
lesson's mirror image: "DLQ category dominance ≠ still happening,"
always check `dead_at`/timestamp distribution against known fix dates
before concluding a failure pattern is current). Full account:
`technical-debt.md`'s round-61 entry (corrected in place).

---

## DLQ Alert Redesign: Growth-Rate Over Lifetime-Count (Round 61)

**Date:** 2026-08-20

**Context:** The false-positive-forever mechanism above (`DeadLetterQueueGrowing`
firing on a monotonic, unfiltered `dlq_size` lifetime count) is a real
design flaw, separate from any of the underlying failure causes. User's
explicit ask: fix what's actually causing the DLQ to fill up first (done —
see the captcha and politeness entries above), then come back and design
the alert properly — with one hard constraint: **keep getting alerted
when something is actually wrong.** Silencing or loosening the alert was
never on the table.

**Options considered** (presented to the user): (1) growth-rate alert
only; (2) growth-rate alert + a separate low-priority informational nudge
about lifetime table size; (3) same as (2) plus actually building
retention/archival for old DLQ rows. User picked (2).

**Decision:** `DeadLetterQueueGrowing`'s expression changed from
`dlq_size > 100` to `increase(dlq_size[1h]) > 20`
(`monitoring/alerts/prometheus_rules.yml`) — the exact same pattern the
file's pre-existing `CircuitBreakerFrequentTrips` rule already used for
`circuit_breaker_trips_total`. No metrics/code change was needed:
`dlq_size` is already effectively monotonic under the current no-purge
DLQ design (see the round-61 entries above), so Prometheus's own
`increase()` windowing gives an accurate "how many new failures landed in
this hour" reading for free. This is the general lesson from the
`proxy_exhausted` correction above applied directly to the alert itself —
a raw lifetime count can't distinguish "still happening" from "happened
once, forever recorded" without a time window.

New `DeadLetterQueuePileLarge` (`dlq_size > 2000`, `severity: info`) is
explicitly NOT a duplicate active-incident alert — it's a periodic
housekeeping nudge that the lifetime table is getting large, routed
(new `alertmanager.yml` `severity: info` match) to a 24h repeat interval
instead of the 4h default so it can't nag like a real problem. Both
thresholds are starting points pending real production volume data.

**Rejected for this round:** actual DLQ retention/archival (option 3).
`storage/dlq.py::clear()`'s docstring states permanent-forever is
intentional (audit trail) — changing that is a data-retention policy
decision with its own tradeoffs (compliance/audit needs vs. table
bloat), not something to bundle into an alerting fix. Left open, see
`.wolf/STATUS.md` → "Next phase".

**Verification:** `promtool check rules` and `amtool check-config` both
run clean against the new files (the amtool `unsupported scheme` warning
on `${SLACK_WEBHOOK_URL}` is pre-existing — confirmed via `git stash` that
it fails identically against the unmodified file; substitution happens at
`docker-entrypoint.sh`, not statically). `prometheus`+`alertmanager`
restarted to load the new config. **Live-verified via Prometheus's own
`/api/v1/alerts` and `/api/v1/query` endpoints**: `dlq_size` still reads
610 (unchanged, confirming this is genuinely the same stale data, not a
coincidentally-resolved count) but neither `DeadLetterQueueGrowing` nor
`DeadLetterQueuePileLarge` appears in the active alert list — the old
`dlq_size > 100` expression would still be firing at this exact instant.

**Status:** Shipped and live. Not yet committed (round 60's `5d8c7df`
still HEAD).
