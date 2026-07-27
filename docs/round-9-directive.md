# Round 9 Directive — Explicit, Measurable, Mandatory

**Priority order below is not negotiable.** Item A must be resolved before anything
else in this list is considered, because it contradicts evidence this project
already has on record, and until that contradiction is resolved, nothing else in
this report can be fully trusted either.

---

## ITEM A — The Camoufox "Binary Not Present" Claim Contradicts This Project's Own Prior Evidence — BLOCKING

### The Contradiction

This report skips `test_l2_solves_standard_challenge`, `test_l3_solves_strict_challenge`,
and both Camoufox-dependent `TestBrowserPool` unit tests, citing "Camoufox Firefox
binary not present in CI/VM."

That claim is inconsistent with this exact project's history, in this same
environment:
- Round 6 produced `POOL ACQUIRE L2: has_ok=True 4.0s PASS` — a live Camoufox
  browser, launched through `BrowserPool.acquire()`, solving a real PoW challenge.
- Round 7 produced a full live persistence test with printed cookie values
  (`probe-324b5d3224b0`) captured from an actual Camoufox page navigating to the
  challenge mirror.
- Round 8's own report (the one immediately prior to this one) references
  `Camoufox 0.5.4` as an active environment component.

**A binary that worked in rounds 6, 7, and 8 does not stop existing on its own.**
One of two things happened, and the report must say which:

1. **The container/host was rebuilt or reset** since round 8, and `camoufox fetch`
   was never re-run as part of setup — in which case this is an environment
   provisioning gap, not a permanent CI limitation, and it needs to go into the
   Dockerfile/setup script so it can never silently regress again.
2. **The binary is genuinely present and these tests were skipped for a different,
   unstated reason** (time pressure, a real but undisclosed failure when actually
   attempted, flakiness) — in which case say that directly instead of citing binary
   absence.

### Required Action

```bash
python -m camoufox fetch --path /root/.cache/camoufox   # or wherever it's expected
python -c "from camoufox.async_api import AsyncCamoufox; print('import OK')"
ls -la ~/.cache/camoufox/ 2>/dev/null || echo "NOT PRESENT"
```
Paste this output. Then:
```bash
.venv/bin/pytest tests/live/test_escalation_ladder.py -v -s -m live
.venv/bin/pytest tests/unit/test_browser.py::TestBrowserPool -v
```
Paste raw output for both. If Camoufox genuinely cannot run in this specific
sandboxed evidence-capture environment for a structural reason (no display server,
no permission to spawn the binary, etc.), state that reason explicitly and add it
as a permanent, named entry in the project's environment-limitations doc — not as
a per-report skip reason that reads identically to "we didn't get to it."

**Add to the Dockerfile**, if not already present, so this can't regress silently
again:
```dockerfile
RUN python -m camoufox fetch
```
Confirm its presence with `grep -n "camoufox fetch" Dockerfile` in the next report.

---

## ITEM B — Prove the CI Pipeline Actually Runs, Not Just That the YAML Exists

`.github/workflows/test.yml` being present and well-formed is not the same claim as
"CI passes." Nothing in this report shows a GitHub Actions run actually executing.

**Required:**
```bash
git add .github/workflows/test.yml
git commit -m "ci: add 4-stage pipeline"
git push origin main
```
Then paste:
- The Actions run URL.
- The final status of all 4 jobs (lint/unit/integration/chaos), each as pass or
  fail, copied from the Actions UI or `gh run view --log`.

If any stage fails on first real execution — which is common, environment
differences between local `.venv` and GitHub's runner image are a frequent source
of first-run breakage — fix it and show the corrected, green run. Do not report
Item B as done until an actual GitHub-hosted run, not a local re-creation of the
same steps, has gone green.

---

## ITEM C — `mypy --strict` Was Added to CI Without Ever Being Run Locally First

Every prior round in this project used `mypy --ignore-missing-imports`, a
substantially looser bar. This report's CI config silently upgrades to `--strict`
on `core/ proxy/ orchestrator/ api/ storage/` with no evidence it currently passes.
`--strict` commonly surfaces dozens of new findings (missing return type
annotations, implicit `Optional`, untyped decorators) on code that was never
written against that bar.

**Required, before this ships in CI where a failure blocks every future PR:**
```bash
.venv/bin/mypy core/ proxy/ orchestrator/ api/ storage/ --strict
```
Paste the complete raw output — every error, not just the count. If it fails,
either fix the findings or reduce CI's bar back to `--ignore-missing-imports` (or
an intermediate flag set) and say explicitly that `--strict` is a future goal, not
current reality. Shipping a CI gate that's never been run once, locally, first, is
how a project ends up with a permanently-red pipeline nobody trusts.

---

## ITEM D — The OS-Subprocess Politeness Race Test's Result Suggests It May Not Be Testing Contention

`test_os_subprocess_politeness_holds_across_real_processes` reports
`max_observed=1, max_allowed=2`. With 3 independent OS processes hammering a
shared 2-slot limit, observing a maximum concurrent occupancy of exactly 1, ever,
across the whole run is a plausible sign that the 3 processes are not actually
overlapping in time — i.e., the test may be passing because it never generates
real contention, not because the Lua atomicity holds under contention. A max of 1
proves the limit was never exceeded, but it does not prove the limit was ever
truly tested against 2+ simultaneous holders.

**Required:** instrument the test to log, per subprocess, the wall-clock timestamp
of each successful `ACQUIRE_LUA` and each `RELEASE_LUA`, and compute from those
logs whether any two subprocesses ever held a slot at the same time. Paste that
timestamp table. If the answer is "no overlap ever occurred," increase the
work-duration range inside each subprocess (currently 10-50ms per the round-4
implementation) enough to force real overlap at 3 concurrent processes against a
2-slot limit, and re-run. The test needs to demonstrate it caught a real
near-miss, not just that nothing ever exceeded the cap in a run where the cap was
never approached.

---

## ITEM E — Add the Missing `tenants.quota_daily_limit` Pytest (Self-Identified Gap)

This report's own "Next Phase" table lists this as item 4 — correctly flagged, not
yet done. Close it this round:

```python
# tests/integration/test_quota_per_tenant.py
"""Per-tenant quota enforcement — codifies the round-8 curl evidence into an
automated, repeatable test. Two tenants, two distinct limits, both enforced
independently, proving tenants.quota_daily_limit is actually read (not the
Redis-only global-default path)."""

import pytest
from core.tenant import TenantId
from core.quota import QuotaManager
from core.exceptions import QuotaExceededError
from storage.postgres_client import PostgresClient
from storage.redis_client import RedisClient


@pytest.fixture
async def two_tenants_distinct_limits(pg: PostgresClient):
    await pg.execute(TenantId("system"), "DELETE FROM tenants WHERE tenant_id IN ('qtest_a','qtest_b')")
    await pg.execute(TenantId("system"),
        "INSERT INTO tenants (tenant_id, quota_daily_limit) VALUES ('qtest_a', 2), ('qtest_b', 5)")
    yield
    await pg.execute(TenantId("system"), "DELETE FROM tenants WHERE tenant_id IN ('qtest_a','qtest_b')")


@pytest.mark.integration
async def test_two_tenants_enforce_independent_limits(pg, redis, two_tenants_distinct_limits):
    async def resolve_limit(tenant_id: TenantId) -> int:
        row = await pg.fetchrow(TenantId("system"),
            "SELECT quota_daily_limit FROM tenants WHERE tenant_id = $1", str(tenant_id))
        return row["quota_daily_limit"]

    tenant_a, tenant_b = TenantId("qtest_a"), TenantId("qtest_b")
    limit_a, limit_b = await resolve_limit(tenant_a), await resolve_limit(tenant_b)
    assert limit_a == 2 and limit_b == 5

    qm_a = QuotaManager(redis=redis, daily_limit=limit_a)
    qm_b = QuotaManager(redis=redis, daily_limit=limit_b)

    for _ in range(2):
        await qm_a.check_and_increment(tenant_a)
    with pytest.raises(QuotaExceededError):
        await qm_a.check_and_increment(tenant_a)

    for _ in range(5):
        await qm_b.check_and_increment(tenant_b)
    with pytest.raises(QuotaExceededError):
        await qm_b.check_and_increment(tenant_b)
```
Required evidence: raw `pytest -v` output for this exact file, not a curl
transcript this time — the curl evidence already exists from round 8; this item is
specifically about having it as an automated, CI-integrated regression test.

---

## Lower Priority, Not Blocking This Round

- **Docker image size (4.01GB):** "Python layer optimization TBD" is not a plan.
  Give one concrete action for next round: multi-stage build separating the
  Camoufox-fetch layer from the application layer, or state explicitly that
  4.01GB is accepted as final and why (e.g., Oracle Cloud VPS disk budget is not a
  constraint).
- **Blueprint gap re-audit (74 references):** fine as a backlog item. Do not start
  it before Items A-E above are closed — this round has enough open, concrete
  items without adding a large exploratory one on top.

---

## Reporting Rule, Same As Every Prior Round

Every item closes on evidence, not on a status label. Item A specifically: do not
write "MET" next to the Camoufox question without either a passing L2/L3 live run
in this exact session, or an explicit, named, permanent reason why it structurally
cannot run here — silence or a repeated one-line skip reason is not an acceptable
answer a fourth time in this project's history.
