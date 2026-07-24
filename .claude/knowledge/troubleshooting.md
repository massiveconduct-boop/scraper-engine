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
