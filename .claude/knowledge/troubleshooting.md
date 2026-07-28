# Troubleshooting & Known Bugs

**Purpose:** Diagnostic patterns, known failure modes, and their fixes.
**Scope:** Bugs encountered during 7 rounds of audit. Recurring failure patterns.
**When to read:** Debugging failures; encountering familiar error patterns.
**Related:** `.claude/knowledge/decisions.md`, `docs/round-6-*.md`

---

## Recurring Bug Classes

### F-02: TYPE_CHECKING Import Used at Runtime
**Symptom:** `NameError: name 'X' is not defined` at runtime, but ruff/mypy pass clean.
**Root cause:** Symbol imported only under `if TYPE_CHECKING:` but called/instantiated in function body.
**Occurrences:** `fetcher/level_2.py` (CamoufoxWrapper), `fetcher/level_3.py` (CamoufoxWrapper), `browser/pool.py` (CamoufoxWrapper).
**Fix:** Move import out of TYPE_CHECKING block. Do not just add a real import — also check for ALL other TYPE_CHECKING imports in the same file that might have the same bug.
**Detection:** `grep -rn 'if TYPE_CHECKING' --include='*.py' . | while read f; do ... done` — audit script in `standards.md`.

### SIM105: try/except/pass → contextlib.suppress
**Symptom:** Ruff SIM105 warning.
**Fix:** Replace `try: ... except Exception: pass` with `with contextlib.suppress(Exception): ...`.
**Notable locations:** `browser/pool.py` (shutdown, zombie cleanup), `proxy/harvester.py` (tempfile cleanup).

### E501: Line Too Long in Embedded Scripts
**Symptom:** Ruff E501 on f-strings containing Python subprocess scripts.
**Fix:** Add `# ruff: noqa: E501` at file top with comment explaining why.
**Notable locations:** `proxy/harvester.py` (broker subprocess script strings).

---

## Infrastructure Failures

### PgBouncer Connection Refused (Port 6432)
**Symptom:** `ConnectionRefusedError: [Errno 111] Connect call failed ('127.0.0.1', 6432)`.
**Fix:** `docker compose up -d pgbouncer`. PgBouncer must be explicitly started — it's not started by `docker compose up -d postgres redis` alone.
**Impact:** G-05 test errors, suite drops from 170 to 165.

### Suite Regression (165 instead of 170)
**Symptom:** Test count drops 5 from expected 170.
**Diagnosis:** Check which tests ERROR (not FAIL). Errors during collection (PgBouncer down, Redis down) cause test files to silently drop.
**Fix:** Ensure all three infrastructure services are running: `docker compose up -d postgres redis pgbouncer`.

### PgBouncer "wrong password type"
**Symptom:** `asyncpg.exceptions.ProtocolViolationError: server login failed: wrong password type`.
**Root cause:** edoburu/pgbouncer generates MD5 userlist; Postgres 16 requires SCRAM-SHA-256.
**Fix:** `docker compose down -v && docker compose up -d` — pgbouncer-init auto-regenerates SCRAM userlist.

---

## Bash/CI Failures

### Exit 144
**Symptom:** Command returns exit code 144.
**Meaning:** 128+16 = signal 16. The Bash tool in this session sends signal 16 when its 120s timeout expires. This is NOT a kernel OOM (137), NOT SIGTERM (143), NOT a subprocess crash.
**Fix:** Split long commands (>120s cumulative) into separate Bash invocations. Use `ctx_execute` for commands needing >120s.
**Production impact:** None. Docker containers have no execution deadline. The harvester runs as a standalone process with no timeout wrapper.

### Broker Subprocess "Hangs"
**Symptom:** Exit 144 during harvest, no proxy output.
**Diagnosis:** Was misdiagnosed as "broker subprocess hangs." Broker actually works fine (EXIT 0, 3 proxies in ~20s) when run in isolation. The full harvest sequence (Docker startup + judge server + harvest + pool query) exceeds the Bash tool's 120s timeout.
**Fix:** Run harvest through `ctx_execute` (no Bash timeout). Or split harvest into direct-only fast path (5s) and broker slow path (later).
**Verification:** Check broker stdout/stderr. If EXIT 0 with valid proxies, broker is fine — timeout is the issue.

---

## Proxy/Harvest Failures

### Empty Pool After Harvest
**Symptom:** `SELECT count(*) FROM proxy_pool` returns 0 after harvest.
**Causes (check in order):**
1. **Column name mismatch:** INSERT uses `anonymity` but schema has `anonymity_level`. Check with `SELECT column_name FROM information_schema.columns WHERE table_name='proxy_pool'`.
2. **ON CONFLICT mismatch:** INSERT uses `ON CONFLICT (ip, port)` but constraint is `UNIQUE (ip, port, protocol)`. Check with `SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint WHERE conrelid='proxy_pool'::regclass`.
3. **Judge not running:** Check `curl http://127.0.0.1:8089/`. Start with `python judge_server.py &`.
4. **All proxies failed validation:** Normal for free proxies. Check pool query for score distribution.

### All Pool Proxies Score 25
**Symptom:** Pool query shows `avg=25`, no score-60 rows.
**Meaning:** No proxy passed HTTP validation. All are TCP-only (below L1 threshold 40 — cannot be selected).
**Why:** Free proxy HTTP forwarding rate is ~0.02%. Expected behavior. Broker path produces validated proxies (score 60).
**Fix:** Ensure `harvest_once()` calls both paths. Wait for `promote_tcp_only()` re-validation.

---

## Browser/Pool Failures

### Double-Issue Bug
**Symptom:** Two sequential `acquire()` calls return the same context object.
**Root cause:** acquire() re-queued items before selecting — selected item stayed in queue.
**Fix:** Classify-once pattern. Every item classified as selected/keep/teardown exactly once.
**Test:** `tests/unit/test_browser.py::TestAcquireDoubleIssue` — catches reoccurrence.

### Camoufox OOM in pytest
**Symptom:** pytest hangs or crashes on test collection for files importing `browser.*`.
**Root cause:** pytest collection triggers module imports. `browser.camoufox_wrapper` imports `camoufox.async_api.AsyncCamoufox` → Firefox binary loading.
**Fix:** Mark Camoufox-dependent tests with `@pytest.mark.skip`. Run them standalone via `python -c` when Camoufox available.

---

## Force-Push Recovery Patterns (Round 11)

### Test Files Silently Not Collected (Untracked in Git)
**Symptom:** Test count drops from 209 to 197. No collection errors — files simply not discovered.
**Root cause:** `git reset --hard` reverts the working tree to a prior commit. Files created after that commit become untracked. Pytest only discovers tracked files in the working tree.
**Diagnosis:** `git status --short` — look for `?? tests/` lines. These files exist on disk but are not in the index.
**Fix:** `git add tests/<path>` for each untracked test file. Verify with `pytest --collect-only -q`.
**Occurrence:** Round 11 — 5 test files lost: `tests/unit/test_promotion.py`, `tests/unit/test_session_isolation.py`, `tests/live/test_session_persistence.py`, `tests/integration/test_promotion.py`, `tests/integration/test_quota_per_tenant.py`. 13 tests. All restored via `git add` + commit.

### Production Code Reverted by Force-Push
**Symptom:** Previously-working tests fail with signature errors, import errors, or missing attributes.
**Root cause:** `git reset --hard <old-commit>` also reverts production files that were modified in later commits. Test files that import those modules then fail at runtime (not collection time).
**Diagnosis:** Check key production files for reverted content. Compare function counts: `grep -c "def\|class" <file>` against expected. Check for missing parameters (`__init__` signature changed), missing methods, wrong backends (Redis vs Postgres).
**Fix:** Restore each reverted production file from prior evidence. Check: `browser/session_state.py` (Redis→Postgres), `browser/pool.py` (logging + session wiring), `browser/camoufox_wrapper.py` (storage_state constructor), `api/routes.py` (SSRF+quota+DB wiring), `api/main.py` (lifespan vs on_event), `observability/metrics.py` (REGISTRY + count_validated_proxies), `core/quota.py` (tenant_id in key), `api/auth.py` (revoked_at), `storage/postgres_client.py` (public in search_path).

### InFailedSQLTransactionError After search_path Fix
**Symptom:** `asyncpg.exceptions.InFailedSQLTransactionError: current transaction is aborted, commands ignored until end of transaction block`.
**Root cause:** `PostgresClient.acquire()` wraps `SET search_path` + yield in `BEGIN...COMMIT`. If the first query after `SET search_path` fails (e.g., `UndefinedTableError` because `public` schema was excluded from the path), the transaction is aborted. All subsequent queries in the same `acquire()` block fail with `InFailedSQLTransactionError`.
**Fix:** Always include `public` in search_path: `SET search_path = {tenant_str}, public`. The `proxy_pool` table lives in `public` schema, not per-tenant schemas.
**Occurrence:** Round 11 — `test_promotion.py` fixture tried `DELETE FROM proxy_pool` with search_path set to only `system` (no `public`). First query failed → transaction aborted → cleanup SET search_path also failed → cascade error on next acquire.

### UndefinedTableError: relation "proxy_pool" does not exist
**Symptom:** `asyncpg.exceptions.UndefinedTableError: relation "proxy_pool" does not exist`.
**Root cause:** Same as above — `SET search_path` set to tenant schema only, missing `public`. `proxy_pool` is a global table in `public` schema.
**Fix:** `SET search_path = {tenant_str}, public` — tenant schema first (so per-tenant tables shadow public if name collision), public as fallback.

### SessionStateManager.__init__() got unexpected keyword argument 'pg'
**Symptom:** `TypeError: SessionStateManager.__init__() got an unexpected keyword argument 'pg'`.
**Root cause:** `browser/session_state.py` was reverted to the old Redis-based version (`__init__(self, redis: RedisClient)`). The test file and `browser/pool.py` expect the Postgres-based version (`__init__(self, pg: PostgresClient, ttl_days: int = 30)`).
**Fix:** Restore the Postgres-based `SessionStateManager` from round 7 evidence. Signature must be `__init__(self, pg: PostgresClient, ttl_days: int = 30)`. Internals: `load`/`save`/`delete` use `self._pg.acquire(tenant_id)`, query `browser_sessions` table.

---

## CAPTCHA Provider Gotchas (Round 19)

### NoCaptchaAI public docs are STALE — use live-verified task forms
The docs at docs.nocaptchaai.com under-specify/mis-state several tasks. Live-probed
correct forms (createTask accepted, HTTP 200 errorId 0):
- **ImageToText:** image field is `image`, NOT the docs' `body` (`body` → `ERROR_INVALID_TASK_DATA "No images found"`). Solves SYNCHRONOUSLY (solution in the createTask response). `solution.text` is a **list**.
- **reCAPTCHA v2:** `ReCaptchaV2TaskProxyLess` (casing: ReCaptcha…ProxyLess). Docs' `RecaptchaV2TaskProxyless` is accepted but sits `idle` forever (no solver).
- **Cloudflare Turnstile:** `AntiTurnstileTask`. Docs' `TurnstileTaskProxyLess`/`CloudflareTurnstileTaskProxyLess` → HTTP 400 "Payload not valid".
- **GeeTest v4:** `captchaId` field. Docs' `gt`/`challenge` (v3) → "No images found".
- **MTCaptcha:** `MTCaptchaTask` accepted.
- **AWS WAF:** `AWSWAFTask` needs per-request runtime data (awsKey/awsIv/awsContext/awsChallengeJS) extracted from the live page — no static site key. Synthetic input → "Payload not valid".

### Captcha task accepted but stuck `status:"idle"` forever (root-caused round 22)
Round 19's "wrong casing → idle forever" (line above) is real but incomplete —
round 22 proved the **correct** `ReCaptchaV2TaskProxyLess` casing/format also
sits at `idle` forever, so a right-looking request is not proof the task will
ever solve. Confirmed via raw `createTask`/`getTaskResult` calls (bypassing
this repo's wrapper) against two different real sitekeys — Google's demo AND
2captcha's demo, ruling out "one specific test key is filtered" — both gave
`errorId: 0` + a real `taskId`, then `status: "idle"` on every poll for 45+
seconds straight, no error ever raised. Root cause: `GET
https://api.nocaptchaai.com/balance?apiKey=...` (the *current* balance
endpoint — richer than the legacy `POST /getBalance` this repo's
`get_balance()` calls) returns `plan: {planType: "", planId: "", ...}` and
`is_default: 1` — **no subscription plan, wallet-balance-only account**.
NoCaptchaAI's pricing page confirms only pay-as-you-go *packages* ($10/50K
solves+) grant "REST API access" + worker slots; a plan-less account has none,
even with real wallet balance. Interactive/browser-rendered types (reCAPTCHA
v2, Turnstile, GeeTest, MTCaptcha) need a worker slot to render+solve the
widget; ImageToText doesn't (pure ML inference on a submitted image) — which
is exactly why ImageToText solves for real money on this same key while
everything else sits idle forever. Not a demo-sitekey artifact, not a code
bug — verified the request format is byte-for-byte what NoCaptchaAI's current
docs show. Fix: buy a package at nocaptchaai.com/manage. Diagnostic:
`services/nocaptcha.py::NoCaptchaAIClient.has_active_plan()` (added round 22)
calls the current `/balance` endpoint and is wired into
`tools/validate_captcha_keys.py`, which now reports `NO PLAN` instead of a
misleading `WORKING` for this exact situation. Full evidence trail:
`.claude/knowledge/decisions.md` → "CAPTCHA Solver" round-22 follow-ups #2/#3.

### CapSolver fallback non-functional — key check (superseded round 21, re-confirmed round 22)
Round 19 saw HTTP 401 `ERROR_KEY_DENIED_ACCESS`. **Round-21 re-check
(`tools/validate_captcha_keys.py`) corrected this:** the current
`CAPSOLVER_API_KEY` now AUTHENTICATES — `getBalance` returns `0.0`. So the key is
valid; the fallback is non-functional because the account has **$0.00 balance**
(can't pay for solves), not because the key is rejected. Fix = top up, not
replace. Lesson: `getBalance` succeeding proves the key/account, NOT that solving
will work (needs funds + active capability) — grade on balance, and confirm a
real solve with `tools/verify_captcha_live.py`. Not a code issue. Round 22: a
live reCAPTCHA v2 solve attempt (NoCaptchaAI returning `None` → falling
through to CapSolver) surfaced `ERROR_KEY_DENIED_ACCESS` again at actual
task-creation time even though `getBalance` authenticates — consistent with
$0 balance being rejected earlier at createTask than at the balance check;
does not change the fix (top up), just confirms it end to end.

### Fetcher `fetch()` argument order — url FIRST, tenant SECOND
`Level1Fetcher.fetch(url, tenant_id, proxy=None, overrides=None)` takes the URL
first (`fetcher/level_1.py:32`). Calling `fetch(tenant_id, url)` (tenant-first, the
intuitive order) passes the tenant slug as the URL → httpx raises
`"Request URL is missing an 'http://' or 'https://' protocol"`, classified as
`NETWORK_TIMEOUT`. This looks like a broken/proxy-less engine but is a caller bug.
Verified the engine works with the correct order: L1 fetch of a real site →
`success=True, http_status=200`, full HTML. Note the Worker uses
`_fetch_url(tenant_id, url_str, level)` internally (tenant-first) — do not confuse
the two signatures.

---

## Docker Image Ships Camoufox but Can't Launch a Browser (Round 13)

**Symptom:** app imports fine, camoufox binary present, but a real browser fetch
fails inside the container. Chain of errors, each a missing runtime dep the minimal
`slim` base lacks:
1. `camoufox.exceptions.CannotFindXvfb` → add `xvfb` (production config uses headless_mode=virtual)
2. `NotInstalledGeoIPExtra` → install `camoufox[geoip]` (config geoip=true), not plain camoufox
3. `libgtk-3.so.0: cannot open shared object file` → add `libgtk-3-0`
4. `BrowserType.launch: Failed` → add `libx11-xcb1`

Also: Camoufox fetches to `/root/.cache/camoufox`, NOT `/root/.camoufox` (stale
pre-round-13 Dockerfile path). Surfaced only by running the browser suite IN-container
(`docker run --network host … pytest tests/chaos/test_safe_content_guard.py`).

**4GB image export exceeds the harness 120s command cap.** Build detached:
`nohup docker build -t <tag> . > /tmp/build.log 2>&1 &` — orphaned process ignores
the tool timeout; poll the log / `docker images` across turns.
