# Scraper Engine — Round 6 Directive
### Mandatory Requirements. No Reinterpretation. No Partial Credit.

This is not a review. This is an instruction set. Every item below has a pass/fail
condition stated in advance, and "PARTIALLY MET," "infrastructure is in place," or
"documented as a limitation" are not acceptable outcomes for anything marked
**BLOCKING** below. If a requirement cannot be met, the report must say so explicitly
under a heading titled `UNRESOLVED — REQUIRES DECISION FROM PRODUCT OWNER`, with the
specific blocker named — not folded into a "limitations" section and called done.

---

## 0. Round 5 Verdict, For The Record

Real work happened: `BrowserPool` F-02 caught and fixed before it shipped (third
occurrence of the same bug class — the pattern-matching worked), proxybroker2 is
genuinely producing proxies now, PgBouncer SCRAM is wired and tested through the
actual pooler port, and the worker.py coverage arithmetic explanation is credible
this time (executable-statement counting, not a retyped excuse). Credit given.

But look at the pattern across six rounds: **every single time a blocking item gets
hard, the fix that ships is the smallest thing that makes the test pass, not the
thing the blueprint specified.** Blueprint v2 called for 50+ proxy sources for
resilience — round 5 shipped 2, both still able to correlate-fail together, and
called it "infrastructure in place for expansion." The blueprint's proxy scoring
model was supposed to differentiate proxy quality — round 5 ships a TCP-connect
probe (which only proves a port is open, not that the proxy forwards HTTP traffic)
and assigns it the same kind of score a judge-validated proxy gets. This is scope
erosion happening one reasonable-sounding shortcut at a time. Each individual
shortcut has a defensible-sounding reason. The cumulative effect, six rounds in, is
a system that passes its own tests while being measurably smaller than what was
designed. **That stops this round.** Below is exactly what "done" means for each
open item — not "acceptable for now."

---

## 1. Proxy Source Diversity — BLOCKING

**Current state:** 2 sources (proxyscrape, geonode). Blueprint requirement: resilient
to any single source failure. Two sources that can both go dark simultaneously
(as literally just happened to all 8 previously-configured sources at once) is not
resilience — it's a slightly-delayed single point of failure.

**Exit criterion (measurable, no interpretation):** Minimum **6 independently-operated
proxy sources**, each with its own parser, each proven live and returning ≥1 proxy
in the same harvest run, in the same report. Not "infrastructure supports adding
more" — 6 sources returning proxies, in one `harvest_once()` call, with per-source
counts printed.

**Specific sources to integrate (pick at least 4 more beyond the current 2 — these
are real, currently-operating free proxy list endpoints as of this instruction; verify
each is still live before integrating, and drop any that are dead with the dead
source logged, not silently skipped):**
- `https://www.proxy-list.download/api/v1/get?type=http` (plain-text list API)
- `https://api.openproxylist.xyz/http.txt` (plain-text list)
- `https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt` (community-maintained, updated frequently — GitHub-hosted, unlikely to correlate-fail with the API-based sources above)
- `https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/http.txt` (same rationale — GitHub raw content, different failure domain than commercial proxy-list APIs)
- `https://www.freeproxy.world/api/proxy?...` (if still live — verify)
- proxybroker2's own default provider list, but **only the subset that Layer-3 analysis
  in round 4 already confirmed can return results in isolation** (i.e., re-verify each
  of the 38 individually with a short timeout, keep only the ones that produce results,
  document exactly which ones and why the rest are excluded — not "all 38 are broken,"
  a per-provider pass/fail table)

**Required evidence format:**
```
harvest_once() source breakdown:
  proxyscrape_http:    N proxies
  proxyscrape_https:   N proxies
  geonode:              N proxies
  proxy_list_download:  N proxies
  thespeedx_github:     N proxies
  monosans_github:      N proxies
  TOTAL (before dedup):  N
  TOTAL (after dedup):   N
```
If fewer than 6 sources return ≥1 proxy, the report must say exactly that — which
sources were attempted, which returned zero, and why (dead, changed format, rate
limited) — not roll the shortfall into a summary "PARTIAL" line.

---

## 2. Proxy Validation — BLOCKING

**Current state:** TCP connect probe only. This proves a port accepts a TCP
handshake. It does **not** prove the endpoint forwards HTTP traffic, doesn't inject
ads/malware, doesn't return a captive-portal page for every request, or isn't a
honeypot. A TCP-open, HTTP-non-functional proxy is worse than no proxy — it will be
selected by `ProxyManager` (which only checks `reliability_score`), consume a
politeness slot, and fail the scrape, burning the retry budget for no reason.

**Exit criterion:** Every proxy inserted into `proxy_pool` MUST pass a real HTTP
round-trip through the proxy to a judge endpoint before being assigned a score at or
above the Level 1 threshold (40). Specifically:

```python
async def _http_validate(proxy: Proxy, timeout: float = 5.0) -> tuple[bool, AnonymityLevel]:
    """
    Makes an actual HTTP GET THROUGH the proxy to a judge URL (e.g. httpbin.org/get
    or a self-hosted judge — self-hosted is strongly preferred so this doesn't also
    become a dependency on a third party's uptime). Must verify:
      1. Request completes within timeout (proves HTTP forwarding actually works)
      2. Response body is parseable JSON matching the expected judge response shape
         (proves it's not a captive portal / injected content / hijacked response)
      3. Inspect returned headers for X-Forwarded-For / Via / Proxy-Connection to
         classify anonymity_level (transparent/anonymous/elite) — this field exists
         in the schema and has been sitting unpopulated this whole time; populate it.
    Returns (is_valid, anonymity_level). Never returns True on TCP-connect success alone.
    """
```

**Two-tier scoring, enforced in the DDL/insert logic, not just documented intent:**
- TCP-probe-only proxies: inserted with `reliability_score = 25` (below the Level 1
  threshold of 40 — cannot be selected until promoted)
- HTTP-judge-validated proxies: inserted/updated with `reliability_score = 60` and
  correct `anonymity_level`
- A background promotion job re-checks TCP-only proxies with the HTTP validator on
  a schedule and promotes on success — do not just leave them permanently capped.

**Required evidence:** Query `proxy_pool` after a harvest cycle and paste the raw
output of:
```sql
SELECT anonymity_level, COUNT(*), AVG(reliability_score), MIN(reliability_score), MAX(reliability_score)
FROM proxy_pool GROUP BY anonymity_level;
```
If `anonymity_level` is still 100% `'transparent'` (the schema default) after this
change, the HTTP validator is not actually running — that's the tell, check for it
yourself before submitting the report.

---

## 3. BrowserPool — Full Lifecycle, Not Just `acquire()` — BLOCKING

**Current state:** `acquire()` → launch → PoW solve → content is live-proven (good).
`release()` and `shutdown()` are unit-mocked only. This is exactly the seam where
F-16 (Playwright driver process leak) and F-14 (unbounded browser spawn) live — the
two most severe findings from the original audit — and the two methods responsible
for closing them have never actually run against a real Camoufox process.

**Exit criterion:** A single live test that does all of the following and asserts on
real OS process counts, not mocks:

```python
# tests/live/test_browser_pool_lifecycle.py
import psutil, asyncio

async def test_pool_full_lifecycle_no_leak():
    pool = BrowserPool(tenant_id=TenantId('lifecycletest'), prewarm_count=2)
    await pool.start()

    def camoufox_process_count():
        return sum(1 for p in psutil.process_iter(['name']) if 'camoufox' in (p.info['name'] or '').lower()
                   or 'firefox' in (p.info['name'] or '').lower())

    baseline = camoufox_process_count()
    assert baseline >= 2, "prewarm did not actually launch processes"

    wrapper = await pool.acquire(proxy=None)
    async with wrapper as ctx:
        page = await ctx.new_page()
        await page.goto('http://127.0.0.1:8090/?difficulty=standard', timeout=30000)
    await pool.release(wrapper, healthy=True)

    # Force an unhealthy release path too — this is the branch that's never been exercised
    wrapper2 = await pool.acquire(proxy=None)
    await pool.release(wrapper2, healthy=False)  # must NOT return to pool, must NOT leak process

    await pool.shutdown()
    await asyncio.sleep(1)  # allow OS to reap
    final = camoufox_process_count()
    assert final == 0, f"LEAK: {final} camoufox/firefox processes still running after shutdown()"
```

**Required evidence:** raw stdout of this test passing, plus `ps aux | grep -i
camoufox` (or firefox) executed immediately before and after the test run, pasted
verbatim, showing the count go to zero.

---

## 4. PgBouncer SCRAM Regeneration — Automate It, Don't Document The Manual Step As Done — BLOCKING

**Current state:** userlist.txt regeneration is a manual command a human has to
remember to run after every Postgres container recreation. This is a guaranteed
future outage — the exact class of "works today, silently breaks on next deploy"
bug this whole audit exists to prevent.

**Exit criterion:** The regeneration must run automatically, every time, with no
human step. Implement as a Postgres container `entrypoint` wrapper or a
`docker-compose` `depends_on` + init-container pattern that runs before PgBouncer
starts:

```yaml
  pgbouncer-init:
    image: postgres:16-alpine
    depends_on:
      postgres:
        condition: service_healthy
    volumes:
      - ./infra/pgbouncer:/pgbouncer-config
    entrypoint: >
      sh -c "
        until pg_isready -h postgres -U scraper; do sleep 1; done;
        SCRAM=$$(psql -h postgres -U scraper -d scraper_engine -t -A -c \"SELECT rolpassword FROM pg_authid WHERE rolname='scraper'\");
        echo \"\\\"scraper\\\" \\\"$$SCRAM\\\"\" > /pgbouncer-config/userlist.txt
      "

  pgbouncer:
    depends_on:
      pgbouncer-init:
        condition: service_completed_successfully
    # ... existing config
```

**Required evidence:** `docker compose down -v && docker compose up -d` (full teardown
including volumes, forcing Postgres to regenerate its role and password hash from
scratch) followed immediately by a successful `psql -h localhost -p 6432 -U scraper -c
"SELECT 1"` with **zero manual commands run in between**. If a human has to type
anything between `docker compose up` and the successful connection, this item is not
done.

---

## 5. httpx/aiohttp Conflict — Resolve The Open Question, Don't Carry It Forever

**Current state:** Correctly labeled "plausible, unconfirmed" — that labeling itself
is fine and should stay if the question stays open. But it shouldn't stay open
forever; it's a one-hour isolation test.

**Exit criterion — pick ONE:**
(a) Prove it: create `HarvesterMinimal`, a class identical to the real
`ProxyHarvester` in every way (same file, same method, copy-pasted) except with the
`httpx` import removed and replaced with nothing (no substitute import). Run the
identical broker call. If it now returns proxies, the diagnosis is confirmed — report
it as confirmed, not plausible.
(b) Disprove it: if `HarvesterMinimal` still returns 0, the httpx import was never
the cause, and the real cause is still unknown — say so explicitly, and note that the
subprocess-isolation fix is still correct and should stay regardless, but the
"root cause" section of the round-4 report needs a correction, not a footnote.

No third option. "Plausible, unconfirmed" is allowed to be the final answer only
after this specific test has actually been run and is inconclusive — not as a
permanent substitute for running it.

---

## 6. Worker.py — One More Check, Then This Is Genuinely Closed

The executable-statement explanation is credible. Close it out with one thing that's
been asked for twice and not yet delivered: **the actual `htmlcov/z_*_worker_py.html`
file itself**, attached or pasted as raw HTML, not a hand-transcribed table of what it
shows. A transcription is still a claim about the evidence; the file is the evidence.

---

## 7. Things That Are Genuinely Done — Do Not Re-Litigate These

To be clear about what's NOT being reopened: the sync SHA-256 fix, the queue-based
proxybroker2 API fix, the subprocess isolation architecture, the OS-subprocess
politeness race test, the SSRF redirect-chain test, and the `TYPE_CHECKING` audit
methodology are all solid and should be built on, not redone. This directive is about
closing the specific 5 items above, not restarting the project.

---

## 8. Format Requirement For The Next Report, No Exceptions

1. Every claimed pass condition must include the **raw, unedited terminal output**
   that produced it — piped directly from the command, not retyped into a markdown
   table. If a table is included for readability, the raw output must also be
   present above or below it.
2. Every "PARTIAL" or "documented limitation" must name the specific blocking
   sub-condition and who needs to make a decision to unblock it (this doc's product
   owner, or a follow-up engineering task) — not be left as an open-ended caveat.
3. Any claim of the form "X is functionally equivalent to Y" (e.g., "direct Postgres
   is structurally equivalent to PgBouncer for this test") must be accompanied by
   either a test that actually exercises Y, or a retraction of the equivalence claim.
   This exact pattern has already produced one incorrect PASS label in this project
   (G-05, round 4) — it does not get a second pass at face value.

**Next round is measured against the six numbered exit criteria above. Nothing
softer than what's written there counts as closing an item.**
