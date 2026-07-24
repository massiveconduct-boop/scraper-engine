# Round 6 Verification — 2 of 6 Cleanly Closed, 1 Regression, 3 Missing Required Evidence

**Verdict: this report does not get to claim "6/6 MET."** Two items are genuinely
closed. One item introduced a regression that isn't explained anywhere in the
report, sitting quietly in the last section as if it were routine. Three items are
marked "MET" with implementation code shown but **without the specific raw evidence
the directive required by name** — a code snippet is not the same thing as the
transcript proving the code ran and did what it claims. This is the same pattern
flagged in round 5, applied to a different set of items. It stops here, again.

---

## STOP — Unexplained Regression, Fix Before Anything Else Is Reviewed

> `165 passed, 2 skipped, 1 warning, 1 error in 18.39s`

Round 5's final suite: **168 passed, 2 skipped, 1 warning, 0 errors.**
Round 6's final suite: **165 passed, 2 skipped, 1 warning, 1 error.**

That's **3 fewer passing tests and 1 new error**, introduced somewhere in this
round's changes, and it is not mentioned, explained, or even acknowledged anywhere
in the report — it's sitting in the "Full Verification" section as if a suite
shrinking by 4 tests' worth of outcome is a footnote. An "error" in pytest (as
distinct from a "failure") almost always means a **collection error** — an import
that blows up before the test even runs, which would also explain why the total
count dropped: if a test *file* fails to import, every test in that file silently
disappears from the count instead of showing as failed.

**Required before I'll accept any other item in this report:**
```bash
pytest tests/unit/ tests/integration/ tests/chaos/ -v --tb=long 2>&1 | grep -A 30 "ERROR\|error"
```
Paste the full traceback. My working hypothesis, in priority order, given what
changed this round: (a) `tests/live/test_browser_pool_lifecycle.py` importing
`psutil` or `BrowserPool` at module level and failing to collect in an environment
without the Camoufox binary present, or (b) `harvester.py`'s new `httpx`-based
`_http_validate()` breaking an existing mocked harvester unit test that assumed the
old TCP-only insert path. Confirm which, fix it, and report the suite back at 169+
passed / 0 errors (169, not 168 — a new test file was added this round, the total
should have gone *up*, not down).

---

## Item-by-Item

### Item 1 — Proxy Source Diversity: **NOT MET as stated**

The directive's exit criterion, verbatim: *"6 independently-operated proxy sources,
each... **returning ≥1 proxy in the same harvest run**."* The report's own raw
output shows geonode returned 0 in the harvest run presented as evidence. That is
5 of 6, in the run that was supposed to prove 6. The "UNRESOLVED" section then
tries to credit geonode anyway because "it worked in a prior round" — that is
precisely the kind of retroactive credit the directive was written to foreclose.
**This item is not met until a harvest run shows 6 sources each returning ≥1 proxy
in that one run, or a 6th replacement source is substituted for geonode and proven
live in the same run.**

Separately, and worth an explicit answer, not a shrug: `openproxylist.xyz` returned
**1** proxy in the harvest but the source-verification curl found **5,732** matching
IP:port patterns on the same endpoint. That's a 5,732:1 ratio between "available"
and "harvested." Either there's a hardcoded limit truncating this source far below
its actual yield, a parser only matching the first line, or a rate limit silently
capping the response — find out which and say so. Don't leave a 3-orders-of-magnitude
discrepancy unremarked in a report whose whole premise is precision.

### Item 2 — HTTP Validation: **NOT MET — required evidence missing, and one explicit directive instruction was ignored**

Two problems, one of them a direct miss of something the directive said in as many
words:

1. **The directive said:** *"a self-hosted judge is strongly preferred so this
   doesn't also become a dependency on a third party's uptime."* The implementation
   validates every proxy against `http://httpbin.org/get` — a third party. This
   means Item 1's entire point (don't depend on any single external service for
   proxy availability) has been reintroduced one layer up, at the validation stage:
   if httpbin.org is down, rate-limits, or blocks the datacenter IP ranges this host
   likely sits in, **every single proxy in the pool gets capped at score 25
   forever**, regardless of how many sources fed it, because none can ever pass
   validation to reach the L1 threshold of 40. This isn't a hypothetical — httpbin.org
   is a widely-used, frequently-rate-limited public test service, exactly the
   profile of thing that breaks under any real volume. Either stand up a
   self-hosted judge (a five-line HTTP server that echoes headers — genuinely
   simpler than the challenge mirror already built for BD-05) or give an explicit,
   argued reason why httpbin.org is acceptable despite the directive's stated
   preference. Silently using it without addressing the instruction is not
   acceptable.

2. **The directive's required evidence was:** *raw output of `SELECT anonymity_level,
   COUNT(*), AVG(reliability_score)... GROUP BY anonymity_level`.* This query output
   does not appear anywhere in the report. Implementation code was shown; the
   database was never actually queried and shown in the report. Run it and paste it.
   If `anonymity_level` comes back 100% `'transparent'`, that's the tell that the
   validator isn't actually populating the field despite the code existing — check
   for that specifically before resubmitting.

### Item 3 — BrowserPool Lifecycle: **NOT MET — required evidence missing, and the regression may be sitting right here**

The directive's required evidence was explicit: *"raw stdout of this test passing,
plus `ps aux | grep -i camoufox` executed immediately before and after the test
run, pasted verbatim, showing the count go to zero."* None of that appears. What's
shown is the test's source code, which is well-written, but a well-written test
that was never proven to execute is exactly as unverified as no test. The phrase
"Requires Camoufox runtime for full execution" reads as a hedge that this test may
not have actually run to completion in the environment that produced this report —
if that's the case, say so plainly instead of marking the item "MET." Given the
unexplained suite regression above, there's a real chance this test file is the
source of the new collection error. **Resolve the regression first — it may
resolve this item's missing evidence at the same time.**

### Item 4 — PgBouncer Auto-Entrypoint: **NOT MET — required evidence missing**

The directive asked for one specific, unambiguous transcript: `docker compose down
-v && docker compose up -d`, immediately followed by a successful `psql -h
localhost -p 6432 -U scraper -c "SELECT 1"`, with the raw terminal output of that
exact sequence and nothing manual run in between. The report substitutes "Tested
with existing SCRAM regeneration flow" — a sentence, not a transcript. The
docker-compose YAML looks correct on inspection, but this is precisely the kind of
change (init-container ordering, `depends_on` conditions, volume mount paths) that
looks right and doesn't work on the first real attempt more often than not. Run the
exact command sequence and paste the output.

### Item 5 — httpx/aiohttp Conflict: **Accepted, with one cheap strengthening ask**

This meets the letter of the directive's exit criterion (a): `HarvesterMinimal`
without httpx returned proxies. Accepted as CONFIRMED. One low-cost improvement,
not blocking: run both variants — with-httpx and without-httpx — inside the same
script, same session, back to back, and show `0` then `5` (or similar) in one
output. A single run of the without-httpx variant alone doesn't fully rule out
ordinary proxy-source flakiness (these sources have already been shown to vary
run-to-run) as an alternative explanation for the earlier `0` result. Cheap to add,
removes the last sliver of doubt.

### Item 6 — Worker.py Coverage HTML: **Accepted**

Raw `coverage -m` output matches prior rounds' numbers exactly, file existence and
size are plausible and consistent with the claimed content. This is fine. Minor
note for the future: an `ls -la` of a file is not the file — if there's a way to
paste or attach the actual HTML (or at least the specific `<span>` lines around
75-76, 85, 130-174 showing the coverage CSS classes) in a future report, that
closes the gap between "the file exists" and "the file shows what's claimed."

---

## Summary

| # | Item | Verdict |
|---|---|---|
| — | Suite regression (168→165 passed, +1 error) | **BLOCKING — unexplained, fix first** |
| 1 | 6+ proxy sources | **NOT MET** — 5/6 in the actual run; retroactive credit not accepted |
| 2 | HTTP validation | **NOT MET** — required SQL evidence missing; directive's self-hosted-judge preference ignored without justification |
| 3 | BrowserPool lifecycle | **NOT MET** — required test-execution evidence missing |
| 4 | PgBouncer auto-entrypoint | **NOT MET** — required down/up/connect transcript missing |
| 5 | httpx/aiohttp conflict | **MET** (strengthening ask, non-blocking) |
| 6 | Worker.py coverage HTML | **MET** |

**2 of 6 actually closed. Do not resubmit the other four with the same code and a
firmer adjective in the status column — resubmit with the specific missing
evidence, or an honest statement of why it can't be produced, for each one.**
