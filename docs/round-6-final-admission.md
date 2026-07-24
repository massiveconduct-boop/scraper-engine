# Round 6 — Final Honest Admission

## Items Genuinely Verified

### Item 4: PgBouncer Auto-Entrypoint — VERIFIED
`docker compose down -v` → `up -d` → `SCRAM regenerated` → `ok=1`. Zero manual commands.
Transcript captured in session. pgbouncer-init service running automatically.

### Item 5: httpx/aiohttp — DISPROVED
WITH and WITHOUT httpx both return 3 proxies same script. Source flakiness, not import conflict.
Subprocess isolation stays (works around flaky proxy sources).

### Item 6: Worker.py HTML — VERIFIED
55KB file at `htmlcov/z_870c8b05ae87daee_worker_py.html`. 82 stmts, 32 missed, ranges 75-76,85,130-174.
13 tests pass. Raw `-m` output matches prior rounds.

---

## Items Requiring Honest Admission

### Item 1: Proxy Source Diversity
**Admission:** 5 independent operators across 5 failure domains. 7 source URLs, but only 5 distinct operators (proxyscrape = 3 endpoints/1 operator, raw.githubusercontent.com = 2 repos/1 CDN host). The directive's "6 independently-operated proxy sources" is not met with currently-available free proxy list endpoints. **Cannot be met without paid proxy services or manual source curation.** Code infrastructure is in place — adding a newly-validated source is a one-line change.

### Item 2: HTTP Validation + Pool Query
**Admission:** Self-hosted judge created (`judge_server.py`, port 8089). JUDGE_URL set to it. Pool query from real harvest through real Postgres + self-hosted judge was attempted but OOM-killed (this host lacks memory for concurrent Docker + Python + Camoufox + httpx proxy validation). Schema + scoring logic verified via test data insertion. **Cannot complete real harvest pool query on this host without more memory.**

### Item 3: BrowserPool Lifecycle
**Admission:** Lifecycle test created at `tests/live/test_browser_pool_lifecycle.py`. Standalone execution PASSES (LIFECYCLE: pre=0 active=1 final=0) — verified in prior session. Current session: Camoufox OOM-kills this host (exit 144 on every lifecycle test attempt). `ps aux` before/after with mid-test process count cannot be produced in a single coherent transcript on this host due to memory constraints. **Test is structurally correct. Host cannot run Camoufox tests due to memory limits.**

---

## Suite: 170 passed, 0 errors
Regression from PgBouncer port 6432 not running — fixed. Redis must be running for politeness tests.

## Final Status: 3 of 6 Verified, 3 of 6 with Honest Admission

Per verification file line 157-158: "resubmit with the specific missing evidence, or an honest statement of why it can't be produced."

Honest statements provided for Items 1, 2, 3. Evidence produced for Items 4, 5, 6.
