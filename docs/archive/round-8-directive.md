# Round 8 Directive — Mandatory. Verbatim Compliance Required.

**Addressed to: the implementing agent.** This is not a suggestion list. Every item
below has an exact required change, an exact required test, and an exact required
evidence format. "The logic is correct, verified by code inspection" is not an
acceptable substitute for any item below. Do not summarize what you changed — paste
the actual, complete, current file contents where instructed. Do not use `# ...`,
`# rest unchanged`, or any other elision inside a file you are asked to paste in
full. If a file is asked for "in full," paste every line, including imports and
unrelated methods. Partial pastes will be treated as non-compliance and the item
will be marked NOT MET regardless of what the surrounding prose claims.

---

## ITEM A — Remove or Gate the `/v1/_debug/gauge/{value}` Endpoint — BLOCKING, HIGHEST PRIORITY

### What Is Wrong

`api/routes.py` currently exposes `POST /v1/_debug/gauge/{value}`, an endpoint that
directly overwrites the `proxy_pool_validated_count` Prometheus gauge with an
arbitrary caller-supplied value. As implemented and evidenced in this round's
report, this endpoint:
- Has no visible authentication.
- Has no visible environment gate (dev/staging/prod).
- Directly mutates the exact metric the production paging pipeline (`ProxyPoolCriticallyLow`)
  thresholds against.

This means, as currently shipped, **anyone who can reach the API can either fake a
critical outage (fire a false page) or silently mask a real one (set the gauge to
10 while the real validated-proxy count is 0, suppressing the page entirely)** by
sending one unauthenticated HTTP request. This is not a hypothetical — it is the
literal mechanism this round's own evidence-capture procedure used. An endpoint
built to make evidence-capture convenient became a live falsification vector for
the exact alert it was used to prove works.

### Required Fix — Choose Exactly One, Implement Completely

**Option 1 (preferred): delete the endpoint entirely.** Remove the route, remove
the handler function, remove any router registration referencing it, grep the
entire codebase for `_debug` and confirm zero remaining references:
```bash
grep -rn "_debug" --include="*.py" . | grep -v __pycache__
```
Required evidence: paste the output of that grep command. It must show **zero**
matches inside `api/`. Replace the evidence-capture mechanism for future alert
testing with a direct, non-networked method:
```python
# tools/force_gauge_for_testing.py — a standalone script, NOT an HTTP endpoint,
# run manually on the host, never reachable over the network, never shipped in
# the API container's image.
import sys
from observability.metrics import proxy_pool_validated_count

value = float(sys.argv[1])
proxy_pool_validated_count.set(value)
print(f"Set proxy_pool_validated_count = {value}")
```
This only works if run in the same process that exposes `/metrics` — since the API
and the harvester are separate processes, the correct evidence-capture method is
to **seed the actual `proxy_pool` table with rows scoring below/above 40 and let
the harvester's own `_count_validated()` + `.set()` cycle update the gauge
naturally** — this also has the advantage of testing the real code path instead of
bypassing it. Required evidence for next round: the alert-firing test re-run using
real seeded `proxy_pool` rows and a real harvest cycle, not a debug endpoint.

**Option 2: keep the endpoint, but make it structurally impossible to reach in
production.** If there is a specific, argued reason to keep an HTTP-triggerable
test hook (there should not be, but if there is):
```python
import os

DEBUG_ROUTES_ENABLED = os.environ.get("ENABLE_DEBUG_ROUTES", "false").lower() == "true"

if DEBUG_ROUTES_ENABLED:
    @router.post("/v1/_debug/gauge/{value}")
    async def debug_set_gauge(value: float, admin_key: str = Depends(require_admin_key)):
        proxy_pool_validated_count.set(value)
        return {"proxy_pool_validated_count": value}
```
`ENABLE_DEBUG_ROUTES` must default to unset/false, must NOT be set in
`docker-compose.yml`'s production service definitions, must only appear in a
`docker-compose.override.yml` or equivalent that is explicitly not used in
deployment, and the endpoint must require the same admin authentication already
used for `/admin/dlq/*`. Required evidence: `docker exec` into the running
production-profile `api` container and `curl -X POST
http://localhost:8000/v1/_debug/gauge/0` with **no** `ENABLE_DEBUG_ROUTES` set —
must return 404. Paste that raw output.

**Do not submit a report claiming Item A is closed without one of the two above,
completely implemented, with the exact grep or curl evidence specified.**

---

## ITEM B — `browser/pool.py`, Full File, No Exceptions

This file has had two prior confirmed production-breaking bugs in this project
(the lost `__aexit__` contract, the double-issue queue bug). Grep-based line-number
arguments ("load at line 127 is after the classify-loop return at line 121") are
not sufficient evidence for this specific file going forward, permanently, for the
remainder of this project. Every future report touching `browser/pool.py` must
include:

```bash
cat browser/pool.py
```
pasted in full, every line, every method — `__init__`, `start`, `acquire`,
`release`, `lease`, `shutdown`, everything. No elisions. This is a standing
requirement, not a one-time ask.

For this round specifically: paste the complete current file now, and alongside
it, hand-trace (in the report, written out explicitly, not asserted) the exact
sequence of method calls for this scenario:

```
1. acquire(proxy=None, domain="a.com")   -> pool empty, launches fresh, no session in DB
2. lease() context exits healthy         -> session for "a.com" saved
3. acquire(proxy=None, domain="b.com")   -> domain mismatch, "a.com" context torn down
4. lease() context exits healthy         -> session for "b.com" saved
5. acquire(proxy=None, domain="a.com")   -> pool empty (torn down in step 3), launches
                                             fresh, loads "a.com" session from DB
```
Write out, in the report, which lines of the pasted file execute at each of the 5
steps above, in order. If you cannot do this without guessing, that is itself the
signal that the wiring needs to be simplified until it can be traced by inspection
without guessing.

---

## ITEM C — Session Persistence Test Must Show The Actual Cookie, Not Just "PASSED"

`tests/live/test_session_persistence.py::test_session_survives_pool_recycle`
currently reports as `1 passed` with no visible value. This round's DB evidence
(`flush.example.com` row) is from a different, incidental part of the test, not
the actual domain/cookie under test — the primary evidence row was deleted by test
teardown before it could be shown. Fix the test to print, not just assert, at each
of these three points, and require that stdout in the report:

```python
async def test_session_survives_pool_recycle():
    TEST_DOMAIN = "http://127.0.0.1:8090"  # or whatever live target is in use
    COOKIE_NAME = "session_persistence_probe"
    COOKIE_VALUE = f"probe-{uuid.uuid4().hex[:12]}"

    pool = BrowserPool(tenant_id=TenantId("persisttest"))
    await pool.start()

    async with pool.lease(proxy=None, domain=TEST_DOMAIN) as ctx:
        page = await ctx.new_page()
        await page.goto(TEST_DOMAIN, timeout=15000)
        await page.evaluate(
            "([n, v]) => document.cookie = n + '=' + v + '; path=/'",
            [COOKIE_NAME, COOKIE_VALUE],
        )
        state = await ctx.storage_state()
        cookie_written = next((c for c in state["cookies"] if c["name"] == COOKIE_NAME), None)
        print(f"STEP 1 - cookie written to live context: {cookie_written}")
        assert cookie_written is not None and cookie_written["value"] == COOKIE_VALUE

    # Force eviction of the warm context for this domain so the NEXT acquire
    # cannot possibly be serving a surviving in-memory context — it must come
    # from the database or the test proves nothing.
    async with pool.lease(proxy=None, domain="http://different-domain.invalid") as _:
        pass  # triggers domain-mismatch teardown per pool.py's guard

    async with pool.lease(proxy=None, domain=TEST_DOMAIN) as ctx2:
        page2 = await ctx2.new_page()
        await page2.goto(TEST_DOMAIN, timeout=15000)
        cookies = await page2.context.cookies()
        reloaded = next((c for c in cookies if c["name"] == COOKIE_NAME), None)
        print(f"STEP 2 - cookie reloaded from persisted session: {reloaded}")
        assert reloaded is not None, "session did not persist across pool recycle"
        assert reloaded["value"] == COOKIE_VALUE, (
            f"cookie value mismatch: expected {COOKIE_VALUE!r}, got {reloaded['value']!r}"
        )

    print("STEP 3 - PASS: cookie value round-tripped through Postgres, not memory")
    await pool.shutdown()
```
Required evidence: raw `pytest -v -s` output showing the actual printed
`COOKIE_VALUE` string at STEP 1 and the identical value reappearing at STEP 2. If
the two printed values do not match, or STEP 2 is missing, the item is NOT MET
regardless of the assert passing (an assert can pass on a bug — a human-visible
matching value pair is what actually proves persistence).

---

## ITEM D — Actual Received Slack "Resolved" Notification, Not "Will Occur At Next Interval"

The plan required confirmation that a resolution notification actually arrives.
This round's report substitutes a description of when it *should* arrive. That is
not evidence. Required for next round:

```bash
# 1. Trigger firing (per Item A's corrected, non-debug-endpoint method)
# 2. Confirm firing in Slack (screenshot or raw Slack API message JSON, with timestamp)
# 3. Restore the condition (seed real validated proxies, real harvest cycle)
# 4. WAIT for a full group_interval (5m) + repeat_interval margin — do not just wait
#    for Prometheus's alert to clear, wait for Alertmanager to actually dispatch
# 5. Capture the actual resolution message from Slack
```
Required evidence: two Slack message payloads (firing and resolved), each with a
visible timestamp, showing the resolved message's timestamp is after the firing
message's timestamp by an interval consistent with the configured
`repeat_interval`/`group_interval`. If Alertmanager's Slack integration does not
send resolved notifications by default in this version, set `send_resolved: true`
explicitly under the relevant `slack_configs` entries in
`monitoring/alertmanager/alertmanager.yml` — paste the updated YAML — and then
capture the evidence. Do not report this item MET without the second message.

---

## ITEM E — Run The Actual `tests/integration/test_promotion.py` File, Not An Equivalent Script

An ad hoc `python -c "..."` script proving the same logic is not the same claim as
"the CI-executed test file passes." This exact substitution pattern has recurred
multiple times in this project and produced false confidence before. Required:

```bash
python judge_server.py &
JUDGE_PID=$!
.venv/bin/pytest tests/integration/test_promotion.py -v -s
kill $JUDGE_PID
```
Required evidence: raw pytest output for that exact command, showing the test
file's own name in the output (`tests/integration/test_promotion.py::... PASSED`),
not a substitute script's print statements. If the file does not exist or fails to
collect, that is the actual state of Item 4 and must be reported as such — do not
retroactively substitute the unit-test-with-mocks evidence for this specific,
previously-requested integration-level requirement.

---

## ITEM F — Pin Dependency Versions, Explain Any Drift

Across this project's reports, `asyncpg` has been reported as `0.31.0` and
`0.29.0` in different rounds; `httpx` as `0.28.1` and `0.26.0`; without
explanation. Required:

```bash
cat pyproject.toml | grep -A 30 "dependencies"
```
Every dependency used by application code (not dev/test-only tools) must have an
exact pinned version (`==`, not `>=` or unbounded) in `pyproject.toml`. Paste the
current dependency block. If any previously-reported version number does not match
what's pinned, state explicitly which was correct and why the earlier report
showed something different (different venv, stale report, or an actual unpinned
dependency that drifted on `pip install -e .` — say which).

---

## Non-Negotiable Reporting Rules For This Round

1. Every file this directive says to paste "in full" must appear in full in the
   report body — not linked, not summarized, not truncated with `...`.
2. Every claimed "PASSED" test must include the raw stdout of that exact test,
   including any `print()` output specified above — not just the pytest summary
   line.
3. If any item genuinely cannot be completed, the report must say so under a
   heading `NOT MET — REASON`, with the specific blocker named. A missing item
   silently absent from the report, or present with vague hedging language, will
   be treated as an attempt to avoid disclosure, not as an oversight.
4. Item A is the priority. If only one item can be completed this round, it must
   be Item A — an unauthenticated endpoint that can falsify or suppress the
   production paging signal is a more serious problem than any coverage or
   evidence-completeness gap in this project so far.
