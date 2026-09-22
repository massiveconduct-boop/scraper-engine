# Troubleshooting & Known Bugs

**Purpose:** Diagnostic patterns, known failure modes, and their fixes.
**Scope:** Bugs encountered across this project's full history (rounds 1-29
and counting). Recurring failure patterns, not one-off fixes already fully
covered by `technical-debt.md`.
**When to read:** Debugging failures; encountering familiar error patterns;
before assuming a live-test or CI failure means the code under test is
broken.
**Keywords:** bugs, gotchas, diagnostics, known failures, CI failures,
browser/pool failures, proxy/harvest failures, CAPTCHA gotchas, SSRF
diagnostic patterns, import rebinding, type stub drift.
**Dependencies:** none — self-contained diagnostic reference.
**Related:** `.claude/knowledge/decisions.md` (WHY a fix was chosen),
`.claude/knowledge/technical-debt.md` (full round-by-round history a bug
belongs to), `.archive/{evidence,directive,closure}/round-6-*.md`
(local-only, not tracked in git)

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

### Round 40: `except Exception:` Doesn't Catch a Third-Party Library's `sys.exit()`
**Symptom:** A code path documented as "falls back gracefully on failure"
instead crashes the entire job/process, even though it's wrapped in a
`try/except Exception:`.
**Root cause:** Some third-party libraries call `sys.exit(N)` on an
environment-check failure instead of raising a normal exception (e.g.
Botasaurus's `botasaurus_proxy_authentication` → `javascript_fixes.
check_node()`, which `sys.exit(1)`s if Node.js isn't on `PATH`, reached
only when a proxy string carries embedded `user:pass@` credentials).
`sys.exit()` raises `SystemExit`, a `BaseException` subclass — NOT an
`Exception` subclass — so a bog-standard `except Exception:` guard doesn't
catch it, and it propagates all the way up, bypassing any
"catch-and-fall-back" contract in between.
**Fix:** Catch it explicitly where the fallback contract needs to hold:
`except (Exception, SystemExit):`. Deliberately NOT a bare `except:` —
that would also swallow `asyncio.CancelledError` (breaks cooperative job
cancellation) and `KeyboardInterrupt`.
**Occurrence:** `fetcher/level_2.py::_fetch_via_botasaurus` — see
`technical-debt.md`'s round-40 entry for the live incident (one job left
permanently stuck at `PROCESSING` before this fix).
**General lesson:** when a documented "always falls back on failure"
contract seems to not be holding, check whether the failure is actually a
`SystemExit`/other non-`Exception` `BaseException` before assuming the
fallback logic itself is broken.

### Round 40: Botasaurus Authenticated Proxies Need `nodejs` AND `npm`, Not Just One
**Symptom:** A Botasaurus fetch using a `user:pass@host:port` proxy string
fails. Two distinct symptoms depending on which binary is missing: (1) no
`node` on `PATH` → `SystemExit` from `javascript_fixes.check_node()` (see
the entry above); (2) `node` present but no `npm` → silent `sh: npm: not
found` in stdout while installing the `proxy-chain` npm package
(`botasaurus_driver`'s `create_local_proxy()` shells out to `npm install`
the first time it's needed — lazy, not vendored into the image).
**Root cause:** Chrome's `--proxy-server` flag has no native username/
password support, so `botasaurus_driver` spins up a local anonymizing
proxy relay via a Node-based helper to strip and inject the credentials.
This whole code path is unreachable — and therefore its missing
dependencies invisible — for any unauthenticated proxy, which is every
free-pool proxy this system used before round 40.
**Fix:** `Dockerfile`'s `system-base` stage installs both `nodejs` and
`npm` (found one at a time, live, via two separate rebuild-redeploy-retest
cycles — don't assume fixing one is sufficient, verify the actual next
symptom).
**Detection:** `docker compose exec <service> node --version` and `npm
--version` inside the running container; grep worker logs for `npm: not
found` or `Installing 'proxy-chain'`.

### Round 27: Bare Dotted Import Rebinding on Package Rename/Move
**Symptom:** After renaming/moving a package, a bulk import-rewrite looks
complete (ruff/grep for `from X import Y` shows nothing left) but a specific
function silently uses the wrong object at runtime — no error, just wrong
behavior, or in the worst case an `AttributeError` deep in a rarely-hit path.
**Root cause:** `import a.b` binds the name `a` in the local namespace (not
`a.b`), so callers reference it as `a.b.thing`. `import a.b as x` binds `x`
to `a.b` itself (the deepest component), not to `a`. A blind regex that
prepends a new prefix to a bare dotted import (`import a.b` →
`import new_prefix.a.b`) silently changes which name gets bound — any code
still saying `a.b.thing` afterward breaks, but with NO import error, since
`new_prefix` still resolves as a name, just not the one being used later.
**Real near-miss (round 27, src/ layout consolidation):**
`services/_anticaptcha.py` had `import core.budget` at module level (used as
`core.budget.CAPSOLVER_CONCURRENCY`), and the function's own parameter was
also named `budget: CapSolverBudget` — rewriting the import to bind the name
`budget` directly (`from scraper_engine.core import budget`) would have let
the function parameter silently shadow the module-level import inside every
function using it, since Python resolves the innermost binding. Caught by
manually checking every bare dotted import's actual usage site before
touching it, not by any tool.
**Fix:** Never blindly regex-rewrite bare `import X.sub[ as alias]` forms.
For each one: (1) check what name it binds (`X` for `import X.sub`, the
alias for `import X.sub as alias`), (2) check that name isn't already used
as a local variable/parameter name in the same scope, (3) if there's a
naming collision, bind the rewritten import to a **distinct** alias
(`import new.X.sub as sub_module`) rather than reusing the original name.
`from X.sub import Y` (named-symbol imports) don't have this problem — they
never rebind a package name, so those ARE safe to bulk-rewrite.
**Detection:** `grep -rn "^\s*import <pkg>\b"` (note: `^\s*`, not just `^` —
inline imports inside function bodies are indented and a `^import` anchor
misses them) — every hit needs manual review, not automated rewrite.

### Round 27: Dotted-String Module References Invisible to Import Audits
**Symptom:** After renaming/moving a package, all tests pass locally in a
quick spot-check, but a full `pytest` run (or worse, production) fails with
`ModuleNotFoundError` for the OLD package name — even though every `import`/
`from` statement was already updated.
**Root cause:** Several real mechanisms reference a module by a dotted
**string**, not a Python import statement, so grepping for `from X import`/
`import X` never finds them: `unittest.mock.patch("old.module.path")` and
`monkeypatch.setattr("old.module.path", ...)` (both single- and
double-quoted, and split across multiple lines); `rq`'s own job queue
(`queue.enqueue("old.module.task_function", ...)` — the worker process
resolves this string via `importlib` at execution time, with no static
check at all); Scrapy's own `scrapy.cfg` (`default = old.module.settings`)
and `settings.py`'s own `SPIDER_MODULES`/`DOWNLOADER_MIDDLEWARES`/
`ITEM_PIPELINES` string keys.
**Real occurrence (round 27, src/ layout consolidation):** all of the above
were found only because the full test suite was run and failed after the
import-statement rewrite looked complete — most seriously,
`api/routes.py`'s `queue.enqueue("orchestrator.tasks.run_scrape_job", ...)`,
which would have silently broken every real scrape/crawl job in production
had it shipped unfixed (confirmed fixed by submitting a real job through the
rebuilt live API and watching it go `PENDING → COMPLETED`, not just by
re-running tests).
**Fix:** After any package rename/move, run the FULL test suite (not a
subset) before considering the rewrite done — that's what actually surfaces
these. Also worth a targeted grep pass for quoted dotted-path strings
matching the old package names, in both quote styles, across `.py`, `.cfg`,
and any framework-specific settings files.

### Round 27: Stale Third-Party Type Stub Shadowing a Package's Own Inline Types
**Symptom:** Local `mypy --strict` and CI's `mypy --strict` disagree about
the SAME line of code — one says a type needs a generic argument, the other
says it accepts none. Both can't be right; the actual cause is that they're
checking against two different sources of type information.
**Root cause:** A third-party stub package (e.g. `types-redis`) pinned in
local dev dependencies but not installed in CI's environment. Once the real
package ships its own inline types (a `py.typed` marker, e.g. real `redis`
since some version), the third-party stub becomes not just redundant but
actively wrong if it targets an old version of that package — and when both
are installed, mypy can resolve via the stale stub instead of the real
package's own types.
**Real occurrence (round 27):** `types-redis==4.6.0.20241004` (stub target:
redis-py 4.6) vs. real installed `redis==8.0.1` (ships its own `py.typed`).
CI's lint job never installed `types-redis` at all — so CI was always
checking against the correct, real types; the local dev environment was
wrong. Confirmed via `git stash` (reproduced CI's exact errors on the
pre-refactor code too, proving this predated round 27's changes) and via
`pip uninstall types-redis` locally (immediately reproduced CI's errors).
**Fix:** When local and CI mypy disagree, check whether a third-party stub
package is installed in one environment but not the other before assuming
either environment's code is wrong — `pip show <stub-package>`,
`ls .venv/lib/*/site-packages/<real-package>/py.typed` to check if the real
package now ships its own types, and if so, remove the stub rather than
patching code to satisfy it.

### Round 27: cwd-Dependent Config Path Only Breaks Inside a Container
**Symptom:** A documented command (`docker compose exec api alembic upgrade
head`) fails with a connection error inside a container, but the exact same
tool works fine when run from a developer's shell or in CI.
**Root cause:** A config file (`alembic.ini`'s `script_location`) used a
bare relative path, which Alembic resolves against the **process's current
working directory**, not the ini file's own location. Locally and in CI,
the tool always happens to be invoked with cwd = repo root (matching the
ini file's directory), so it silently works by coincidence. Inside a
container built with `WORKDIR /app` and the ini file also at `/app`, it
still happens to work — but `docker compose exec api alembic ...` doesn't
guarantee the exec session's cwd matches `WORKDIR` in every Docker/compose
version, and more generally, any invocation from a different directory
breaks it.
**Fix:** Alembic supports `%(here)s` token interpolation (since 1.11) that
resolves relative to the ini file's own directory instead of cwd:
`script_location = %(here)s/migrations`. Prefer a tool's own built-in
location-independence mechanism over hand-rolled `Path(__file__)` tricks
when one exists.

### Round 29: FastAPI `Header()` marker leaks through when a route function is called directly, not via DI
**Symptom:** Adding a new *optional* `Header(...)`-typed parameter to a
FastAPI route function breaks existing unit tests that call the route as
a plain Python coroutine (`await scrape(request, x_api_key="sk-admin")`)
without touching the new parameter — even though the parameter has a
`None` default and the test never passes it. The failure is often a
confusing downstream `KeyError` on a mock's return value, not an obvious
"missing argument" error.
**Root cause:** `Header(None, alias=...)` as a function default is a
`fastapi.params.Header` marker object (a `FieldInfo` subclass) — FastAPI's
dependency-injection layer resolves it to the real header value (or `None`
if absent) only when the route runs through an actual `Request`. Call the
function directly as ordinary Python, bypassing that DI layer entirely (a
pattern this codebase's route tests already rely on for every route,
`x_api_key="sk-admin"` always passed explicitly), and an omitted parameter
gets the marker object itself as its "default" — which is truthy, not
`None`. Existing required headers (`x_api_key: str = Header(...)`) never
hit this because every test already passes them explicitly; the bug only
surfaces the first time an *optional* Header-typed parameter is added and
some existing test doesn't pass it.
**Real occurrence (round 29):** adding `idempotency_key: str | None =
Header(None, alias="Idempotency-Key")` to `scrape()`/`crawl()` broke two
existing tests whose mocked `pg.fetchrow` returned a quota-limit-shaped
dict for every call — the truthy marker object made the new idempotency
dedup lookup run unexpectedly, and it misread that same mock as a "found a
duplicate job" row, `KeyError`'ing on the missing `job_id`/`status` keys.
**Fix:** Pass the new optional parameter explicitly (e.g.
`idempotency_key=None`) at every direct-call test site that doesn't
specifically exercise it — matching the existing explicit-kwarg convention
this test suite already uses for required headers, rather than adding
runtime `isinstance` workarounds in production code for what is purely a
test-calling-convention gap. If a route gains many optional Header params
over time, consider whether route-level tests should switch to FastAPI's
`TestClient`/`AsyncClient` (real request path, no marker-leak risk) instead
of direct coroutine calls — not done in round 29 since the existing
convention only needed two call sites fixed.

---

## Every Jumia URL Suddenly Fails as `proxy_auth_failed` (Round 65, relabelled Round 66)

**Symptom:** a run that was working starts failing every URL, all
`failure_category: proxy_auth_failed`, `proxy_source: paid_gateway`, error
`Page.goto: NS_ERROR_PROXY_AUTHENTICATION_FAILED`, each after one ~5s
attempt. Before round 66 the same thing was labelled `browser_crash`,
retried, escalated through every level and re-driven by the DLQ reaper.

**Cause:** the paid gateway (DataImpulse) refuses our credentials — seen
live as `407 TRAFFIC_EXHAUSTED` when the plan ran out of traffic. Level
memory sends Jumia straight to the gateway (`levelhint:poolblock:*`) and the
free pool is refused there, so nothing gets through. Since round 66 the
refusal is terminal for the URL, and the DLQ reaper holds these entries
until its gateway probe (`paid_gateway.gateway_accepts_credentials`, cached
120s) gets a 200 — after a top-up they re-drive on their own.

**Check:** one direct request through the gateway from inside a worker:
`docker compose exec -T worker-l1 python -c "…build_gateway_proxy(…)…
httpx.get('https://api.ipify.org', proxy=p.auth_url())"` — use `auth_url()`,
not `url()`: without credentials every answer is `407 NO_USER`, which proves
nothing. A 407 with `TRAFFIC_EXHAUSTED` is the account, not the code. Fix:
top up the plan.

## Host Admission On but Load Still High (Round 65)

**Check live browsers against seats:** `/v1/health` → `browser_capacity.
in_use_units` vs. `ps -eo args | grep camoufox-bin | grep -v contentproc`
in each worker. Browsers far above seats means something launches or keeps
browsers outside a claim. The one found live was parked BrowserPool spares
(fixed: `park_spares=False` under admission). A low `target_units` with high
load is the controller reacting to that outside load, not the cause.

Round 66 found the second cause of "limited but still slow": the controller
itself. It raised one unit per 30s and only below `cpu_pressure_low`, so
after any cut it froze between the marks — live, 2-4 browsers on a host
idling at load 3, pages queued for minutes. Fixed (see decisions.md → "A
Limiter That Only Limits When the Host Is Actually Strained"); the same
symptom now means a real strain reading, so check what else runs on the
host. `docker compose logs api | grep host_capacity_target` prints every
change with the pressure, waiters and in-use numbers behind it.

## The API Is Not On Port 8000 (Round 62)

**Symptom:** `curl http://localhost:8000/v1/health` returns
`{"detail":"Not Found"}` (or an unrelated app's response) while
`docker compose ps` insists the api container is `healthy`.

**Not a bug.** The container listens on 8000 *internally* — which is why
the container healthcheck (`curl -f http://localhost:8000/v1/health`) is
green and why `docker compose exec -T api curl ... :8000/v1/health` returns
the real `{"status":"ok",...}` payload. The host-side published port is
`API_PORT` from `.env`, and on this host it is **8010**, because another
project of the operator's (`deepanalyze_agent-app-1`) already publishes
8000. `deploy-platform-core-1` likewise sits on 8001.

**Do not "free" port 8000** — those are unrelated running services, not
leftovers from this stack.

**Resolve it, don't guess it:**

```bash
docker compose port api 8000        # authoritative host mapping for this stack
sudo ss -ltnp | grep :8000          # what actually holds 8000
docker ps --format '{{.Names}}\t{{.Ports}}' | grep 8000
```

Related: `.wolf/cerebrum.md`'s 2026-08-07 Do-Not-Repeat entry already warned
that 8000 can be held by something else; round 62 pins down the current
owner and the one-command way to check.

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
3. **All validation targets unreachable:** validation goes through the proxy to one of `proxy/harvester.py::JUDGE_URLS` (public IP-echo endpoints — `httpbingo.org`, `api.ipify.org`, `postman-echo.com`). Check each is reachable directly from the `proxy-harvester` process (round 35 — runs inside the `api` container via supervisord, not its own container): `docker exec scraper_engine-api-1 curl -s -o /dev/null -w '%{http_code}\n' http://httpbingo.org/ip` (repeat per URL). All three down at once is unlikely but not impossible — if so, add another independent public IP-echo service to `JUDGE_URLS` rather than waiting.
4. **All proxies failed validation:** Normal for free proxies. Check pool query for score distribution.

### All Pool Proxies Score 25 (100% of them, none ever promoted)
**Symptom:** Pool query shows `avg=25` (or similar low number), zero score-60+ rows,
ever — not just most proxies, literally all of them, indefinitely.
**Root cause (round 32, two layers):** first found the self-hosted judge
server was never running in any real deployment, so every single
`_http_validate()` call failed via connection-refused. Fixing that
uncovered a deeper, architectural issue: a loopback judge (`127.0.0.1`)
can never validate a real third-party proxy at all, running or not —
when a request routes through a forward proxy, the *proxy* resolves
"127.0.0.1" as its own machine, never the machine that made the request.
Confirmed live via real proxies returning their own internal service
responses instead of reaching our judge. Fixed by validating against
public IP-echo endpoints instead (`JUDGE_URLS`, item 3 above) — genuinely
reachable from anywhere. If you still see 100%-score-25 after confirming
those are reachable, it's proxy quality, not validation infrastructure.
**Separately, still true even with the judge running:** free proxy HTTP
forwarding success rate is genuinely low (~0.02% per round 6's own
measurement) — so *some* proxies capping at 25 (TCP-reachable but failed
real HTTP validation) is expected. The distinguishing signal is whether
*any* proxies ever reach 60+ — zero, ever, points at the judge; a nonzero
but small fraction is the expected free-proxy base rate.
**Fix:** confirm the judge is reachable (item 3) first. Only after that,
if scores are still low, this is expected free-proxy-source behavior — wait
for `promote_tcp_only()`/`ProxyPromotionJob` re-validation, or use a paid
proxy source for a higher base rate.

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

### BrowserPool destroyed live browsers on ANY mismatch, not just idle timeout (round 25)
**Symptom:** No exception, no test failure — just measurably worse pool reuse than expected; prewarmed instances disappeared on their first real use.
**Root cause:** `acquire()`'s domain-mismatch and (newly-added, same round) proxy-mismatch branches called `w.__aexit__()` (real teardown) instead of just skipping the item. This directly contradicted the module's own docstring ("Tear-down only on unhealthy release, idle timeout, or explicit shutdown"). Compounding it: a prewarmed wrapper's `_last_domain` starts `None`, and `None != "example.com"` was being read as a mismatch — so prewarming was destroyed on literally its first real acquire() call.
**Fix:** Mismatched wrappers are now kept in the pool as spares (`keep.append(...)`) instead of torn down; only genuine idle-timeout expiry (or explicit unhealthy release/shutdown) destroys a live instance. An unclaimed (`_last_domain is None`) wrapper now matches any domain. Proxy mismatch is deliberately NOT given this same relaxation — see `.claude/knowledge/decisions.md` → "BrowserPool Mismatch Handling" for why that's correct, not an inconsistency, before "fixing" it again.
**Test:** `tests/unit/test_browser.py::TestBrowserPool::test_prewarmed_wrapper_not_evicted_on_first_domain_mismatch`, `::test_mismatched_wrapper_kept_in_pool_not_destroyed`.

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

### acquire()'s finally-block COMMIT masked the real exception and poisoned the connection pool (general case)
**Symptom:** Any failing query inside `PostgresClient.acquire()` surfaces `asyncpg.exceptions.InFailedSQLTransactionError` instead of its own real exception (e.g. `UndefinedTableError`) — masking the actual root cause regardless of *why* the query failed (not specific to the missing-`public` case above). Separately, `proxy-harvester`'s logs showed recurring `asyncio`-logger ERROR lines: `"Resetting connection with an active transaction <asyncpg.connection.Connection object at 0x...>"`.
**Root cause:** `acquire()`'s `finally` block used to run `SET search_path = public` then `COMMIT` unconditionally, regardless of whether the `yield`ed query succeeded or raised. A failed query aborts the transaction server-side; the `finally` block's own `SET search_path` then hit the aborted transaction and raised `InFailedSQLTransactionError` itself — Python's exception-chaining means *that* new exception is what propagates to the caller, not the original one — and since that new exception happened before `COMMIT` ran, the connection returned to the pool still mid-transaction. asyncpg's own `Pool.release()`→`Connection._reset()` safety net (not this codebase) detects an unmanaged open transaction on release and force-`ROLLBACK`s it, logging the "Resetting connection with an active transaction" line — that's where the proxy-harvester noise came from, and it could fire from *any* failed query anywhere in the app that goes through `PostgresClient`, not just a proxy-harvester-specific leak.
**Fix:** `acquire()` now distinguishes the two paths explicitly — `except BaseException: ROLLBACK; raise` (no `SET search_path` attempt, since `ROLLBACK` is always accepted regardless of transaction state and the connection is about to be reset by the pool anyway) vs. `else: SET search_path = public; COMMIT` on the clean path. See `storage/postgres_client.py::acquire` and `tests/integration/test_postgres_client.py::test_acquire_failing_query_does_not_mask_error_or_poison_pool` (asserts both that the *original* exception type surfaces, and that a subsequent unrelated `acquire()` on the same pool succeeds immediately — no poisoning).
**Occurrence:** Found during a production-readiness review — the report separately flagged "transaction poisoning masks the real error" and "recurring error-log noise from proxy-harvester" as two uncertain, possibly-unrelated findings; both turned out to be the same root cause.

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
first (`fetcher/level_1.py:68`). Calling `fetch(tenant_id, url)` (tenant-first, the
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

---

## Observability / Tracing / Metrics Failures (Rounds 24-25)

### structlog + stdlib logging bridging — two distinct traps
**Symptom 1:** `configure_logging()` runs, no exception, but log output is still
plain unstructured text, not JSON.
**Root cause:** `structlog.configure(processors=[...])` only affects loggers
obtained via `structlog.get_logger()`. This codebase's loggers are all plain
`logging.getLogger(__name__)` — structlog's native pipeline never sees them.
**Fix:** Use `structlog.stdlib.ProcessorFormatter` as the formatter on a
`logging.StreamHandler` attached to the *root* logger — this is what actually
intercepts stdlib records and renders them through structlog's processors +
renderer (JSON or console). See `observability/logging.py::configure_logging()`.

**Symptom 2 (only appears once Symptom 1 is "fixed"):** every single log call
now fails to format — `--- Logging error ---` spam, `AttributeError` buried in
the traceback referencing `logger.disabled`.
**Root cause:** `structlog.stdlib.filter_by_level` was included in the shared
processor chain passed to `ProcessorFormatter(foreign_pre_chain=...)`.
`filter_by_level` expects a real `logging.Logger` object with `.disabled` —
foreign (plain stdlib) records passed through `ProcessorFormatter`'s
`foreign_pre_chain` don't supply one the same way, so every call crashes.
**Fix:** Drop `filter_by_level` from the shared/foreign processor chain
entirely — level filtering is already handled by the root logger's own
`.setLevel()`, so it's redundant even when it does work.
**Detection:** if structured-logging output looks right in isolated manual
testing but production/live containers show `--- Logging error ---` blocks,
suspect a processor in the chain that assumes a structlog-native logger.

**Also note:** `logging.basicConfig()` is a no-op once the root logger already
has ANY handler — a very common gotcha, and this codebase's processes always
have one by the time custom setup runs (something else imports first). Set
`root_logger.handlers = [...]` directly instead of relying on `basicConfig()`.

### BatchSpanProcessor + fork() — spans vanish with zero errors anywhere
**Symptom:** Tracing is fully configured (real `TracerProvider`, real spans
created, no exceptions anywhere in the code under test), but a specific
process's spans never show up in the trace backend — while the *identical*
code, run in a fresh one-off process (e.g. `docker exec ... python -c "..."`),
produces a trace immediately. No error message anywhere points at the cause;
this is the hardest kind of bug because everything downstream *looks* correct.
**Root cause:** the process in question forks a child for each unit of work
(here: `rq`'s `Worker.perform_job()`, which the library's own source docstring
says "will/should only be called inside the work horse's process" —
`rq/worker/base.py` confirms the child terminates via `os._exit()`, not a
normal Python exit). Two independent problems compound:
1. `os._exit()` skips `atexit` entirely — an `atexit.register(provider.shutdown)`
   registered before the fork is inherited by the child but never fires.
2. `BatchSpanProcessor`'s background export thread does not survive `fork()`
   at all — only the calling thread continues into the child. The child's
   spans queue into an in-memory buffer with no thread left to drain it.
Both must be true for spans to vanish silently; either one alone would still
usually surface *some* symptom (a log warning, a slow shutdown).
**Fix:** in the forking process's own per-unit-of-work function (not at
module/process level — that already ran once in the pre-fork parent and
won't run again), explicitly call
`trace.get_tracer_provider().force_flush(timeout_millis=<bounded>)` before
that unit of work returns. Pass an explicit bounded `timeout` to the
exporter's own constructor too (e.g. `OTLPSpanExporter(timeout=2)`) —
`force_flush`'s timeout only bounds how long `force_flush` itself waits, not
an export call already in flight against the exporter's own (usually longer)
default deadline.
**Detection method that actually worked:** don't trust "no error in the logs"
as proof either way — query the trace backend's own API directly (e.g.
Jaeger's `/api/traces?service=X&tag=job_id:<id>` for the *exact* unit of
work), and compare the same code path invoked two ways: once through the
normal forking/queueing mechanism, once called directly in a fresh process.
A difference in outcome between those two, with identical code, is the
signature of this bug class.
**Full narrative + the actual live evidence:** `.claude/knowledge/decisions.md`
→ "Decision: `force_flush()` in the rq Job's `finally` Block, Not `atexit`".

### FastAPI `/openapi.json` 500s under concurrent load — `from __future__ import annotations` + locally-scoped import
**Symptom:** A route's return-type annotation (e.g. `-> Response`) causes
`pydantic.errors.PydanticUserError: TypeAdapter[...ForwardRef('Response')...]
is not fully defined` when FastAPI builds the OpenAPI schema — intermittently,
under concurrent requests, not on every call.
**Root cause:** the module has `from __future__ import annotations`, so every
annotation (including return types) is stored as a string, resolved lazily
against the *function's `__globals__`* (module-level globals) when something
actually needs the real type (schema generation does; normal request handling
often doesn't, which is why it doesn't fail immediately). If the type used in
the annotation was only imported inside a nested function's local scope (not
at module level), resolution fails — but Pydantic's internal caching/mock-
validator machinery can make this manifest as an intermittent concurrency
race rather than a deterministic failure on the very first request.
**Fix:** import the type at module level, not inside whichever function
happens to use it as a return annotation.
**Full evidence:** `.claude/knowledge/technical-debt.md` (round 23) — this was
found by the first-ever real run of `tests/load/locustfile.py`, itself a
separate lesson: an unrun load test is not a passing load test.

### Prometheus metric set from worker/harvester code never appears in `/metrics` (round 25)
**Symptom:** No exception anywhere. The `Gauge`/`Counter` object is defined,
imported, and `.set()`/`.inc()` is genuinely called somewhere in the
codebase — a naive "is this wired?" grep looks clean — but the metric never
shows up in a real `curl /metrics`, and its alert rule silently never fires
(no series to evaluate, not an error).
**Root cause:** `prometheus_client`'s `REGISTRY` is in-process global state.
`/metrics` is served by the `api` process; the metric was being set inside
an rq worker process or the `proxy-harvester` daemon — different processes
entirely. Worse for rq specifically: it forks a fresh "work horse" process
per job that `os._exit()`s immediately after, so the metric is gone before
the next scrape could ever see it even in principle.
**Diagnosis:** don't trust "the `.set()` call exists in the code" as proof.
Ask which *process* actually executes that line, and whether that's the
same process that serves `/metrics`. If not, the metric is dead regardless
of how correct the call site looks.
**Fix:** write to Redis/Postgres at event time (from whichever process the
event happens in), refresh the local `Gauge` from that at scrape time
(inside `/metrics`'s handler, in the `api` process only). Full pattern +
which metrics this applies to: `.claude/knowledge/architecture.md` →
"Metrics: Cross-Process Emission Pattern". This is exactly how
`proxy_source_healthy` was missed by the first round-25 audit pass (the
Gauge existing and being called somewhere passed a naive check) and only
caught via a live `/metrics` cross-check against real running containers.
**Detection method that actually worked:** rebuild the real containers,
hit the real `/metrics` endpoint, and grep the output for every metric name
referenced in `monitoring/alerts/prometheus_rules.yml` — don't just confirm
the Python code compiles and the call site exists.

---

## Live-Test Infra Failures (Round 28)

### `tests/live/test_escalation_ladder.py` fails with `FailureCategory.SSRF_BLOCKED`, not an escalation bug
**Symptom:** `test_l1_correctly_fails_against_standard_challenge` (and the
L2/L3 variants, if unskipped) hard-fail with `AssertionError: Expected
200, got None` — looks like a broken escalation ladder.
**Meaning:** It isn't. `result.failure_category ==
FailureCategory.SSRF_BLOCKED` — `core/ssrf_guard.py`'s `DENIED_NETWORKS`
(127.0.0.0/8, 10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16, 169.254.0.0/16,
never weakened for test convenience) rejected the mirror URL before any
fetch happened. `CHALLENGE_MIRROR_URL`'s default (`http://127.0.0.1:8090`)
and this host's own docker-bridge address are *always* in a denied range
by construction — this test can never pass against them, for anyone.
**First (wrong) diagnosis:** concluded a *separate* external VPS was
needed to test the escalation ladder for real, echoing the test file's own
"requires... a real VPS" framing at face value without verifying it.
**Actual fix — point `CHALLENGE_MIRROR_URL` at this host's Tailscale
interface instead:** `100.64.0.0/10` (Tailscale's CGNAT range) is **not**
in `SSRFGuard.DENIED_NETWORKS` at all, and — unlike this host's NAT'd
egress-only public IP (`curl ifconfig.me`-style; times out on self-connect
from the same box, classic hairpin-NAT, a separate unrelated networking
quirk) — it's a real, directly-bound interface, so self-testing actually
works: `ip -4 addr show tailscale0` → `CHALLENGE_MIRROR_URL=http://<that
IP>:8090 pytest tests/live/test_escalation_ladder.py -m live`. Verified
end to end: L1 correctly rejected, L2 solved the standard tier in ~5.1s,
L3 the strict tier in ~13.8s — matching the file's own recorded historical
timings almost exactly. This is what "the real VPS" in the file's original
docstring actually meant.
**Fix applied to the test file itself:** a `_skip_if_ssrf_blocked` helper
now turns the SSRF-blocked case into an honest `pytest.skip` with a clear
reason, instead of a confusing bare assertion failure that reads like a
product bug.
**General lesson:** `result.failure_category` is always the first thing to
check on an unexpected live-test failure before assuming the code under
test is broken — a `SSRFBlockedError`/`NETWORK_TIMEOUT`/etc. failure
category means the *test's own target address* is the problem, not the
escalation ladder.

## Why Did This URL Climb to L3? (Round 64)

Read the result's `escalations` list (`GET /v1/jobs/{id}`, or the
`level_rejected` worker log line). Each entry names the level, the exact
check (`reason`), the HTTP status, the L2 engine, and the proxy source.

- `proxy_source: "pool"` with `status:403` (or `failure:detection_block`)
  and a following `level_N_gateway_retry_ms` in `timings`: the target
  refuses free datacenter exits, not the level. Live on Jumia (round 64),
  forced to L2 the same URLs returned 200 with ~600 links through the
  gateway. Since round 64 that retry happens at the blocked level instead
  of only the last one.
- `signature:<text>` on a page that looks fine to you: a broad literal in
  `ChallengeDetector.CHALLENGE_SIGNATURES` (e.g. `_challenge`,
  `access denied`) matched the site's own markup — a detector false
  positive to fix there, with the captured HTML as the regression test.
- `js_gated`: an SPA shell; escalation to a browser is correct.

- A slow L2 with no rejection at all: grep the worker log for
  `l2_botasaurus_fallback` — Botasaurus failing inside L2 (e.g.
  `CloudflareDetectionException`) before Camoufox answers is not an
  escalation, so it never shows in `escalations`.

Level memory (Redis, 24 h TTL, every 20th URL of a domain re-probes
everything) decides three things per domain: the START level
(`levelhint:{tenant}:{domain}`), whether to skip the free pool
(`levelhint:poolblock:...`) and whether to skip Botasaurus at L2
(`levelhint:botafail:...`). A job that skipped either shows
`pool_skipped_known_block` in the worker log, or no Botasaurus attempt at
all. Delete `levelhint*` for the domain to force a cold, full attempt.
