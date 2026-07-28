# Round 13 — Implementation Plan
### Quick Wins, Real-Target Validation, Production Monitoring, Remaining Backlog

Same standard as every prior round: exact code, exact exit criteria, exact
required evidence. CapSolver live-test excluded per instruction — everything
else from both lists is covered below, organized by actual dependency order
rather than by which list it came from.

---

## PART A — Quick Wins (do these first, they unblock nothing but cost little)

### A1. Config Auto-Injection (DI) — Make the YAML Actually Authoritative Everywhere

**Problem, stated precisely:** `Level2Fetcher`/`Level3Fetcher` accept config
values in their constructors (round 12.2), but every call site has to remember
to pass them. There is currently no single place that guarantees a fetcher
constructed anywhere in the codebase is using `production.yaml`'s values rather
than the constructor defaults. That's a silent-drift risk: a new call site
(a test, a future worker refactor) that forgets to wire config will silently
fall back to hardcoded defaults with no error, no warning.

**Fix — a single factory module, one function per fetcher, called everywhere
a fetcher is constructed:**

```python
# fetcher/factory.py
"""Single source of truth for constructing fetchers from AppConfig.
Nothing outside this module should call Level2Fetcher()/Level3Fetcher()
directly with hand-picked kwargs — that's exactly the drift risk this
module exists to close."""

from __future__ import annotations

from config.schema import AppConfig
from fetcher.challenge_detector import ChallengeDetector
from fetcher.level_2 import Level2Fetcher
from fetcher.level_3 import Level3Fetcher


def build_level2_fetcher(config: AppConfig, challenge_detector: ChallengeDetector | None = None) -> Level2Fetcher:
    lvl = config.levels["level_2"]
    return Level2Fetcher(
        goto_wait_until=lvl.goto_wait_until,
        networkidle_timeout_ms=lvl.networkidle_timeout_ms,
    )


def build_level3_fetcher(config: AppConfig, challenge_detector: ChallengeDetector | None = None) -> Level3Fetcher:
    lvl = config.levels["level_3"]
    return Level3Fetcher(
        goto_wait_until=lvl.goto_wait_until,
        post_load_fixed_wait_ms=lvl.post_load_fixed_wait_ms,
        max_total_wait_ms=lvl.max_total_wait_ms,
        retry_wait_increment_ms=lvl.retry_wait_increment_ms,
        challenge_detector=challenge_detector or ChallengeDetector(),
    )
```

**Enforce it — a grep-based CI check, cheap and effective, same pattern as the
`_debug` endpoint check from several rounds ago:**
```yaml
# .github/workflows/test.yml, add to lint job
- name: no direct fetcher construction outside factory
  run: |
    HITS=$(grep -rn "Level2Fetcher(\|Level3Fetcher(" --include="*.py" . \
      | grep -v "fetcher/factory.py" | grep -v "fetcher/level_2.py" | grep -v "fetcher/level_3.py" \
      | grep -v "^tests/")
    if [ -n "$HITS" ]; then
      echo "Direct fetcher construction found outside fetcher/factory.py:"
      echo "$HITS"
      exit 1
    fi
    echo "OK — all fetcher construction goes through fetcher/factory.py"
```
(Tests are exempted deliberately — unit tests constructing a fetcher directly
with mock args is normal and fine; it's *production call sites* — the worker,
the orchestrator — that must go through the factory.)

**Required evidence:**
1. `orchestrator/worker.py` (or wherever `Level2Fetcher`/`Level3Fetcher` are
   currently constructed in the real escalation path) updated to call
   `build_level2_fetcher(config)` / `build_level3_fetcher(config)`.
2. The CI check above added and passing.
3. One deliberate violation (construct a fetcher directly in a scratch file
   outside `tests/`) to prove the CI check actually fires — same "prove the
   gate catches something" standard applied to the mypy ratchet a few rounds
   back. Paste the failing run, then revert.

---

### A2. Fix the 45 Ruff Errors Across the Broader Project

**Do not fix these by hand one at a time with no record of what changed.**
```bash
.venv/bin/ruff check . --exclude 'challenge-mirror' --exclude 'report-review-fix' --statistics
```
Paste this first — it categorizes the 45 by rule code and count, which tells
you how many are the 17 auto-fixable ones versus the ~28 that need judgment.
```bash
.venv/bin/ruff check . --exclude 'challenge-mirror' --exclude 'report-review-fix' --fix
```
Paste the diff this produces (`git diff --stat`) — auto-fixes should be
mechanical (import sorting, quote style, unused imports) and low-risk, but
"low-risk" still means "show what changed," not "trust it blindly."

For the remaining ~28 non-auto-fixable findings, paste the full list
(`ruff check . --statistics` again, post-fix, showing the reduced count) and
fix them individually, or — for any that are genuinely false positives or
acceptable in context — add a scoped `# noqa: RULE_CODE` with a one-line reason
comment, never a bare blanket suppression.

**Exit criterion:** `ruff check .` (excluding only `challenge-mirror`, which is
A3's job) returns `All checks passed!` with zero remaining findings and zero
newly-added blanket suppressions.

---

### A3. Challenge-Mirror Ruff Pass

Same treatment as the mypy baseline — the mirror is real, deployed code (it's
run in Docker, it's load-bearing for L2/L3 live testing), it shouldn't be
permanently exempt from linting just because it started as a test fixture.

```bash
.venv/bin/ruff check challenge-mirror/
```
Paste full output. Fix what's fixable. If genuine findings remain that are
acceptable for a intentionally-minimal-dependency test fixture (e.g., the
hand-rolled SHA-256 implementation may trip complexity/magic-number rules that
don't apply meaningfully to a from-scratch, already-verified crypto routine),
create `challenge-mirror/.ruff-baseline.txt` following the exact same pattern
as `tools/mypy-baseline.txt` — a committed, named, diffable list, not a blanket
directory exclusion. Update `.github/workflows/test.yml`'s lint job to check
`challenge-mirror/` against that baseline instead of excluding the directory
outright.

---

## PART B — Real, Scoped Work

### B1. The Negative-Control Test Seam — `force_engine="raw_playwright"`

This closes the last permanently-skipped test:
`test_naive_undetected_automation_signal_is_correctly_rejected`. Its entire
purpose is proving that if `Level2Fetcher` ever regresses back to raw,
undetected Playwright (the original F-02/F-03 defect class from the very first
audit of this project), the challenge mirror correctly rejects it. Without this
test, that regression class has no automated guard at all — it would only be
caught by a human noticing, the same way the force-push was only caught by
someone re-running the suite.

**Implementation — an explicit, narrow escape hatch, clearly marked as
test-only, impossible to reach from any production code path:**

```python
# fetcher/level_2.py
class Level2Fetcher:
    def __init__(
        self,
        *,
        goto_wait_until: str = "domcontentloaded",
        networkidle_timeout_ms: int = 5000,
        force_engine: str | None = None,  # TEST-ONLY. See guard below.
    ) -> None:
        if force_engine is not None and force_engine not in ("raw_playwright",):
            raise ValueError(f"force_engine must be None or 'raw_playwright', got {force_engine!r}")
        self._force_engine = force_engine
        self._goto_wait_until = goto_wait_until
        self._networkidle_timeout_ms = networkidle_timeout_ms

    async def fetch(self, url: str, *, proxy=None, tenant_id=None, domain=None):
        if self._force_engine == "raw_playwright":
            return await self._fetch_via_raw_playwright(url, proxy)
        return await self._fetch_via_camoufox(url, proxy, tenant_id, domain)

    async def _fetch_via_raw_playwright(self, url: str, proxy):
        """TEST-ONLY PATH. Launches vanilla Playwright Firefox with NO fingerprint
        spoofing whatsoever — this is deliberately the exact anti-pattern
        Camoufox exists to replace (see blueprint v2 §3.4, F-02/F-03). This
        method exists solely so the negative-control test can prove the
        challenge mirror correctly rejects an undetected-automation session.
        It must never be reachable from any production call path — enforced by
        the __init__ guard above (force_engine only accepts this literal string,
        and production code never passes force_engine at all, per fetcher/factory.py)."""
        from playwright.async_api import async_playwright
        async with async_playwright() as p:
            browser = await p.firefox.launch(headless=True)
            context = await browser.new_context()
            page = await context.new_page()
            await page.goto(url, wait_until=self._goto_wait_until, timeout=30000)
            html = await page.content()
            await browser.close()
            return html
```

**CI enforcement — confirm this path is genuinely unreachable from production,
the same grep-gate pattern as A1:**
```bash
grep -rn "force_engine=" --include="*.py" . | grep -v "fetcher/level_2.py" | grep -v "^tests/"
# must be empty — zero production call sites ever pass force_engine
```

**The test itself, un-skipped:**
```python
# tests/live/test_escalation_ladder.py
@pytest.mark.live
async def test_naive_undetected_automation_signal_is_correctly_rejected():
    """Negative control: raw, unspoofed Playwright must be REJECTED by the
    mirror's navigator.webdriver check. If this ever starts passing (i.e. the
    mirror accepts it), either the mirror regressed or something is wrong —
    this test existing and passing is what proves a Camoufox regression would
    be caught automatically instead of by luck."""
    fetcher = Level2Fetcher(force_engine="raw_playwright")
    result = await fetcher.fetch(f"{MIRROR}/?difficulty=standard")
    # raw Playwright's navigator.webdriver === true by default, unless the
    # test explicitly patches it out — assert the mirror's real rejection path
    assert "navigator_webdriver_true" in result or "Verifying your browser" in result
```

**Required evidence:** raw `pytest -v -s` output showing this test passing
(i.e., proving rejection), plus the grep-gate confirming zero production
reachability, plus re-confirmation that `test_l2_solves_standard_challenge`
(the real Camoufox path) still passes unaffected by this addition.

---

## PART C — Real-Target Validation (My Own Item)

**Framing, stated up front:** every escalation-ladder proof in this project has
been against the self-hosted mirror. That was the right call for iterating
safely and legally, but it leaves one real question unanswered: does this
system actually work against a real commercial anti-bot product, not just a
fair, controllable stand-in? The way to answer this without doing anything
ethically or legally fraught is to test against **infrastructure you own or
explicitly control** — not a arbitrary third party's production site. A few
concrete, legitimate options, cheapest first:

1. **Put a real anti-bot product in front of a site you already own.**
   Cloudflare's free tier includes a real "I'm Under Attack" / bot-fight mode
   that presents a genuine JS challenge, not a simulation of one. Point it at
   any domain you control (a personal project, a parked domain, even a static
   placeholder page), enable bot-fight mode, and run the escalation ladder
   against it. This is the single highest-value, lowest-risk next step —
   it's a real product, real challenge logic, zero ToS ambiguity, because it's
   your own property.
2. **A paid, consent-based bot-detection testing service**, if one exists in
   your budget — some anti-bot vendors offer sandboxed test environments
   specifically for this purpose (verify current offerings, this changes over
   time — I don't have reliable enough information to name a specific current
   vendor with confidence).
3. **A publicly documented "scrape-me" test target**, if a reputable one
   exists and is currently maintained for exactly this purpose — again, verify
   current status rather than trusting anything I might recall, since these
   projects come and go.

**Do not test against a real third party's production infrastructure without
their explicit permission, regardless of how "low-stakes" it seems** — that's
the one hard line here, consistent with everything this project has held to
since the original challenge-mirror decision.

### Implementation — Option 1 (Cloudflare Bot-Fight Mode on Owned Infrastructure)

```yaml
# tests/live/test_real_target_validation.py — NEW, requires explicit opt-in
# env var so this never runs by accident in CI or against the wrong host.
```
```python
import os
import pytest

REAL_TARGET_URL = os.environ.get("REAL_TARGET_VALIDATION_URL")
REAL_TARGET_ENABLED = os.environ.get("REAL_TARGET_VALIDATION_ENABLED") == "true"

pytestmark = pytest.mark.skipif(
    not REAL_TARGET_ENABLED or not REAL_TARGET_URL,
    reason="Real-target validation requires REAL_TARGET_VALIDATION_ENABLED=true "
           "and REAL_TARGET_VALIDATION_URL pointing at infrastructure YOU OWN. "
           "Never enable this against a third party's site without permission.",
)

@pytest.mark.live
async def test_l1_fails_against_real_cloudflare_challenge(harvester, politeness):
    fetcher = build_level1_fetcher()  # or however L1 is constructed via the factory
    result = await fetcher.fetch(REAL_TARGET_URL, ...)
    print(f"L1 vs real target: success={result.success} is_challenge_page={result.is_challenge_page}")
    # No hard assertion on exact outcome yet — this run's job is to OBSERVE and
    # RECORD real behavior, not enforce an assumption about it.

@pytest.mark.live
async def test_l2_l3_against_real_cloudflare_challenge(browser_pool, config):
    l2 = build_level2_fetcher(config)
    l3 = build_level3_fetcher(config)
    r2 = await l2.fetch(REAL_TARGET_URL, ...)
    print(f"L2 vs real target: success={r2.success} duration_ms={r2.duration_ms} category={r2.failure_category}")
    if not r2.success:
        r3 = await l3.fetch(REAL_TARGET_URL, ...)
        print(f"L3 vs real target: success={r3.success} duration_ms={r3.duration_ms} category={r3.failure_category}")
```

**Required evidence, and this is genuinely exploratory, not pass/fail against a
pre-written assertion the way everything else in this project has been:**
raw output of all three levels run against your own Cloudflare-protected
domain, with real timings, real `failure_category` values if it fails, and — if
L2/L3 do fail — the actual HTML/response captured for analysis (does the
current timeout budget need adjusting for a real product's slower/faster
challenge? does `ChallengeDetector` correctly classify Cloudflare's real
interstitial, or does it need the pattern list widened beyond the two strings
added in round 12.3?). **This is the first test in this entire project where
"it doesn't fully work yet" is an expected, useful, non-blocking outcome** —
the point is to find out where the real gap is, not to prove there isn't one.

---

## PART D — Turn Disclosed Gaps Into Watched Production Signals

### D1. Grafana Dashboard + Alert Rules for Everything Already Instrumented

Every metric below already exists in code from prior rounds. None of them have
a dashboard panel or an alert threshold yet — they're emitted into a void.

```yaml
# monitoring/dashboards/production-health.json (Grafana panel definitions, abbreviated to queries)
panels:
  - title: "Validated Proxy Pool"
    query: "proxy_pool_validated_count"
    warn_below: 10
    critical_below: 5   # matches existing ProxyPoolCriticallyLow alert

  - title: "Safe-Content Guard Firings (Level 3)"
    query: "rate(safe_content_none_total[1h])"
    note: "Expected near-zero. Sustained nonzero rate means real targets are
           triggering the mid-navigation race this guard protects against —
           worth investigating the specific target, not just noting the count."

  - title: "CapSolver Daily Spend"
    query: "capsolver_spend_total"
    warn_above_pct_of_ceiling: 80   # matches CapSolverBudgetNearCeiling alert

  - title: "Per-Tenant Quota Utilization"
    query: "quota_current_usage / quota_daily_limit"
    warn_above_pct: 90

  - title: "Circuit Breaker Trips (24h)"
    query: "increase(circuit_breakers_total[24h])"
    warn_above: 5   # matches existing CircuitBreakerFlapping alert
```
```yaml
# monitoring/alerts/prometheus_rules.yml — additive, not replacing existing rules
- alert: SafeContentGuardFiringFrequently
  expr: rate(safe_content_none_total[1h]) > 0.1
  for: 30m
  labels: {severity: warning}
  annotations:
    summary: "Level3Fetcher's _safe_content guard is firing more than incidentally"
    description: "This was previously a theoretical, untested code path (round 12.4).
                   A sustained nonzero rate means it's firing for real — investigate
                   which targets are triggering it before assuming the guard alone is sufficient."
```

**Required evidence:** dashboard JSON committed, alert rule added, and — since
this project's standard is "prove the alert actually fires," not just that the
YAML parses — one deliberate trigger of the new `SafeContentGuardFiringFrequently`
rule (seed the counter directly via the metrics client, same pattern used for
`ProxyPoolCriticallyLow` several rounds back) with the resulting Slack message
pasted.

### D2. Free-Proxy Source Health — a Scheduled Check, Not a One-Time Fact

The 5-6 source count was accepted as a ceiling, but "accepted" needs an ongoing
watcher, not a one-time decision, since these sources go dark independently of
this project's release cycle.

```python
# proxy/source_health.py
"""Runs alongside the harvester loop. Tracks per-source success/failure over
time and exports a gauge per source, so a source going dark shows up as a
specific, named signal — not just a drop in the aggregate pool count that
could have any cause."""

from prometheus_client import Gauge

proxy_source_healthy = Gauge(
    "proxy_source_healthy",
    "1 if the named proxy source returned >=1 proxy in the last harvest cycle, else 0",
    ["source_name"],
)

async def record_source_health(source_name: str, proxy_count: int) -> None:
    proxy_source_healthy.labels(source_name=source_name).set(1 if proxy_count > 0 else 0)
```
Wire this into `ProxyHarvester.harvest_once()`'s existing per-source loop —
one call per source, using data already being computed.

```yaml
- alert: ProxySourceWentDark
  expr: proxy_source_healthy == 0
  for: 6h    # tolerate transient failures; alert on sustained absence
  labels: {severity: warning}
  annotations:
    summary: "{{ $labels.source_name }} has returned zero proxies for 6+ hours"
    description: "One of the (already small) pool of independent proxy sources
                   appears to be dead. Check if it needs replacing per the
                   round-6/6.1 diversity work — this is exactly the class of
                   silent single-source failure that motivated that work."
```

---

## PART E — Aspirational Backlog, Given Concrete Shape

### E1. mypy `--strict` Path Forward — Make the Ratchet Actually Shrink

The standing rule from round 11 ("any PR touching a baseline-listed file must
clean that file's entries") was stated but never turned into an enforced
mechanism. Enforce it:

```bash
# tools/check_baseline_shrinkage.sh — run in CI on every PR
#!/bin/bash
CHANGED_FILES=$(git diff --name-only origin/main...HEAD -- '*.py')
BASELINE_FILES=$(cut -d: -f1 tools/mypy-baseline.txt | sort -u)
for f in $CHANGED_FILES; do
  if echo "$BASELINE_FILES" | grep -qx "$f"; then
    REMAINING=$(grep "^$f:" tools/mypy-baseline.txt | wc -l)
    echo "WARNING: $f is in the mypy baseline ($REMAINING entries) and was modified this PR."
    echo "Per project policy, PRs touching baseline-listed files must reduce that file's entries."
  fi
done
```
This is advisory (warns, doesn't block) rather than a hard gate, since forcing
every incidental touch of a baseline file to also fix unrelated type errors
could itself become a source of scope-creep and rushed fixes — exactly the
failure mode this whole project has been guarding against. A visible warning in
CI output is enough to keep it from being forgotten.

### E2. Docker Image Size — Multi-Stage Split

```dockerfile
# Dockerfile — multi-stage, separating the rarely-changing Camoufox layer
# from the frequently-changing application layer
FROM python:3.12-slim AS camoufox-base
RUN pip install --no-cache-dir camoufox[geoip]==0.5.4 && python -m camoufox fetch

FROM python:3.12-slim AS app
COPY --from=camoufox-base /root/.cache/camoufox /root/.cache/camoufox
COPY --from=camoufox-base /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
WORKDIR /app
COPY pyproject.toml .
RUN pip install --no-cache-dir -e .
COPY . .
CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000"]
```
**Required evidence:** `docker build` before/after size comparison
(`docker images | grep scraper-engine`), and confirmation the full test suite
still passes against the rebuilt image — a smaller image that breaks something
is not a win.

### E3. `_safe_content` Except-Branch — 30-Day Observation, Not Further Code

No code work here — D1 already wired the dashboard and alert for
`safe_content_none_total`. The action item is procedural: **check the counter
in 30 days.** If it's still zero, the gap stays correctly labeled as
theoretical, and no further chasing is warranted. If it's nonzero, the alert
above will have already surfaced it with the specific target/timing context
needed to investigate for real, rather than trying to force a synthetic
reproduction now.

---

## Priority Order If Doing This Sequentially

1. A1 (config DI) — cheap, closes a real drift risk
2. B1 (negative-control seam) — closes the last permanently-skipped test
3. D1 + D2 (monitoring wiring) — makes every prior round's honest disclosures
   actually watched, not just documented
4. C (real-target validation) — the actual highest-value unknown, but
   deliberately last among the "do it now" items since it depends on you
   deciding which owned-infrastructure option to use
5. A2, A3, E1, E2 — genuine backlog, no urgency, pick up opportunistically

Everything above is scoped narrowly enough to evidence individually — same
standard as every round before this one. No item closes on a status label
without the specific evidence named next to it.
