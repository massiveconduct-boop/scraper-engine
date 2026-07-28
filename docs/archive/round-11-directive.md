# Round 11 Directive — Final Closure. Strict, Explicit, Mandatory.

**This is the round that either earns "production-ready" or doesn't.** Before
touching any of the "available work" backlog items, one thing in the status
update needs to be accounted for precisely, because it doesn't match this
project's own prior numbers and unexplained test-count drift has already once
turned out to be a real bug in this project's history (round 6: 168→165 passed
was a genuine collection error, not noise).

---

## ITEM 1 — Explain The Test Count, Exactly — BLOCKING

Round 9's full suite: **209 collected, 204 passed, 5 skipped, 0 failed.**
This status update: **187 tests pass, 2 pre-existing failures.**

That's not a small drift. 187 + 2 = 189 accounted for — **at minimum 20 tests
have vanished from collection entirely** between round 9 and now, unexplained.
"2 pre-existing failures" is also new information — round 9 had **zero** failures,
only skips. A skip and a failure are not the same category, and something
changed the outcome, not just the count.

**Required, before anything else in this directive:**
```bash
.venv/bin/pytest tests/unit/ tests/live/ tests/chaos/ tests/integration/ -q --collect-only
```
Paste the full collected-test list. Then:
```bash
.venv/bin/pytest tests/unit/ tests/live/ tests/chaos/ tests/integration/ -v --tb=long
```
Paste the complete output — every test name, every result, and the full traceback
for both failures. Then produce a diff: **name every test that was collected in
round 9 (209 total) but is not collected now**, and state the reason for each —
deleted intentionally, moved, renamed, or silently broken at collection time
(e.g., an import error in a test file that makes pytest skip the whole file
without reporting it as a failure — this exact failure mode was diagnosed and
fixed once already in this project, round 6, and needs to be ruled out again here
by name, not assumed absent).

**Do not proceed to Items 2-4 until this is accounted for with an exact,
line-by-line explanation.** A regression of this size going unexplained is a
bigger risk to "production ready" than any item on the available-work list.

---

## ITEM 2 — Fix Both Named Failing Tests For Real — BLOCKING

`test_promotion.py` and `test_quota_per_tenant.py` are not incidental tests —
they are the regression guards for two invariants this project spent multiple
rounds establishing (bounded, judge-validated proxy promotion; per-tenant quota
isolation after a real cross-tenant Redis-key collision was found and fixed).
Leaving them permanently red is not consistent with calling this project done.

### 2a — `test_promotion.py`

**Diagnose first, don't guess:**
```bash
.venv/bin/pytest tests/integration/test_promotion.py -v -s --tb=long
```
Paste the complete traceback. The fixture already starts `judge_server.py` as a
subprocess (`scope="module"`, `subprocess.Popen(["python", "judge_server.py"])`)
— if it's "not running," the fixture itself is failing to start it, or something
changed about how the test is invoked (working directory, missing `judge_server.py`
in the path from the current CI/test-runner context, a port already in use from a
prior test leaving it bound). Identify the exact cause from the traceback, fix
the fixture (e.g., use an absolute path via `pathlib.Path(__file__).parent /
"../../judge_server.py"` resolved correctly, or a `port-already-in-use` retry/
cleanup), and re-run to a passing result — not a re-skip.

### 2b — `test_quota_per_tenant.py`

**Diagnose first:**
```bash
.venv/bin/pytest tests/integration/test_quota_per_tenant.py -v -s --tb=long
```
Round 9's version of this test already contained its own fixture that seeds
`qtest_a`/`qtest_b` directly into `tenants` via `INSERT` — so "tenants table
seeding" as a stated cause is either a new problem (the `tenants` table itself,
or a required column, no longer exists/matches in the current migration state —
check `alembic current` vs `alembic heads`, there may be a pending migration not
applied in whatever environment produced this status update) or a fixture-wiring
problem (the `pg`/`redis` fixtures this test depends on are not defined at the
`conftest.py` level the way this test file assumes). Paste `alembic current`,
`alembic heads`, and the actual traceback, identify which of these it is, and fix
the actual cause — not by loosening the test's assertions.

**Required evidence for both:** raw passing `pytest -v` output, by exact test
file name, in the same session as the fix.

---

## ITEM 3 — L2/L3 Timeout Values Must Be Config-Driven, Not Hardcoded Guesses — BLOCKING

This is flagged in the developer's own status update as a real concern, correctly
identified: "current values work against mirror but could be too short for slow
real targets." That is a production correctness gap, not a nice-to-have. A
hardcoded `page.wait_for_timeout(10000)` tuned to just barely pass against a
self-hosted test fixture is exactly the kind of value that will silently fail
against any real target whose challenge takes 12 seconds instead of 8.

**Required:**
```yaml
# config/production.yaml (or wherever level-specific config already lives per blueprint §8)
levels:
  level_2:
    goto_wait_until: "domcontentloaded"
    networkidle_timeout_ms: 5000
  level_3:
    goto_wait_until: "load"
    post_load_fixed_wait_ms: 10000
    # NEW — required this round:
    max_total_wait_ms: 30000          # hard ceiling before giving up and escalating/failing
    retry_wait_increment_ms: 5000     # if content check fails, wait this much longer, once, before giving up
```
```python
# fetcher/level_3.py — replace the flat 10s hardcode with configurable, bounded retry
async def fetch(self, url, ...):
    page = await browser_context.new_page()
    await page.goto(url, wait_until=self._config.goto_wait_until, timeout=self._timeout_ms)
    waited = 0
    await page.wait_for_timeout(self._config.post_load_fixed_wait_ms)
    waited += self._config.post_load_fixed_wait_ms
    html = await page.content()
    while (html is None or self._content_marker not in html) and waited < self._config.max_total_wait_ms:
        await page.wait_for_timeout(self._config.retry_wait_increment_ms)
        waited += self._config.retry_wait_increment_ms
        html = await page.content()
    return html
```
Replace `"challenge-mirror-ok"` as a hardcoded literal in the fetcher with a
config- or caller-supplied success marker/heuristic — the real
`ChallengeDetector` (per blueprint §3.9) should be making this determination
against real targets, not a string literal that only means something against the
mirror. If `ChallengeDetector` isn't wired into `Level3Fetcher` yet, that's the
actual gap to close here, not the timeout number itself.

**Required evidence:** the live L2/L3 tests against the mirror still passing with
the new config-driven values (not a regression from item 3's own change), plus a
one-paragraph, explicit statement of what real-world timeout budget was chosen
and why (e.g., "30s ceiling chosen because CapSolver's own polling budget in
`core/budget.py` is already 60s for CAPTCHA-tier waits, and Level 3 should fail
fast enough to still leave room for a DLQ write within the overall per-URL
`timeout_seconds` from `ConfigOverrides`").

---

## Confirmed, Not Reopened

Docker image size (4.02GB, BD-02) — accepted, closed, no further action needed
per the standing decision from round 9/10. Do not resubmit evidence for this.

---

## Backlog — Explicitly Deferred, Not Part of This Round's Closure Bar

- **mypy 23→0:** Acceptable to remain ratcheted rather than zero. Requirement
  going forward, stated explicitly so it doesn't become a permanent fixture:
  any PR that touches a file already listed in `tools/mypy-baseline.txt` must
  resolve that file's baseline entries as part of the same PR, shrinking the
  baseline over time rather than only preventing net-new growth. Add this as a
  one-line comment at the top of `tools/mypy-baseline.txt` itself so it's not
  just a verbal understanding.
- **Blueprint gap re-audit (74 BD-/F-/G- references):** Real, but large and
  exploratory. Do not start this in the same round as Items 1-3 above. Track it
  as its own future-dated task, not "available work" competing for attention
  against blocking items.

---

## What "Production Ready" Requires To Be Declared This Round

All three of the following, pasted as raw output in the next report, in this
exact order:
1. Item 1's full accounting — every vanished test named and explained.
2. Item 2's two tests passing, by name, in fresh `pytest -v` output.
3. Item 3's config-driven L2/L3 change, still passing against the mirror, with
   the real-world timeout justification paragraph.

A report that addresses only the "available work" backlog while leaving Items
1-2 unresolved does not close this round, regardless of how much backlog work it
covers — an unexplained 20-test collection drop and two red integration tests
that guard real invariants are disqualifying for a "production ready" claim on
their own, independent of anything else in the project's otherwise strong
history this round.
