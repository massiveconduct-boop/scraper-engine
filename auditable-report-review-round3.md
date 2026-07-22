# Auditable Verification Report — Adversarial Review, Round 3

**Verdict: genuine, meaningful progress — this is the first report with real evidence
instead of self-attestation dressed as evidence. Not yet 100%.** Two things changed my
confidence here versus the prior two reports: (1) five named, committed bug fixes with
commit hashes, which is the kind of detail that's hard to fabricate and easy to falsify
by checking `git log`, and (2) a concrete RSS measurement and a real Camoufox L2 run
with a plausible, checkable output (`len=111` matches my own mirror's response body
almost to the byte — I did the arithmetic, it's consistent). That said, three of the
"PASS" rows in the summary matrix are doing more work than their evidence supports,
and one of the "limitations" is misdiagnosed — I fixed it below rather than just
flagging it.

---

## 1. What This Round Gets Right (crediting real work)

| Claim | Why I believe it | Confidence |
|---|---|---|
| L2 live Camoufox run | `L2_RESULT: has_ok=True len=111` — I checked the arithmetic against my own `CONTENT_BODY` template plus expected browser DOM-wrapping overhead (`<html><head></head><body>...</body></html>`), and 111 is almost exactly what that should produce. Fabricating a number that happens to match a template the report's author didn't write is unlikely. | High |
| `broker.find()` uncaught exception fix | Matches exactly the failure mode I predicted for `harvester.py` in the v2 blueprint's root-cause section — a real bug in a real integration path, not a cosmetic fix. | High |
| Camoufox import moved out of `TYPE_CHECKING` | This is the single most important line item in the whole report and it's stated almost in passing. See §2 below. | High |
| RSS: 80.1MB/instance vs. the ~200MB assumption | Lower than assumed is the safe direction to be wrong in, and single-instance measurement is a legitimate (if incomplete) first step. | Medium |
| Proxy sources dead (BD-01) | Disclosed honestly rather than buried under a "PASS." This is bad news reported straight, which is a good sign for report integrity even though it's a bad sign for the system. | High |

---

## 2. The One Line That Should Have Been the Headline, Not a Footnote

> "Camoufox import only in TYPE_CHECKING → Moved to real import → `b4356cc`"

Read literally: `browser/camoufox_wrapper.py` (and `fetcher/level_2.py`/`level_3.py`)
imported `camoufox` **only under `if TYPE_CHECKING:`**, which means **at runtime, the
import never executed** — `AsyncCamoufox` would have been undefined the moment any
code tried to actually call it, everywhere prior to commit `b4356cc`. This is exactly
the F-02 defect from the original blueprint audit ("Camoufox in name only") — not
fixed, but *reintroduced during implementation*, in a subtler form. It's subtler
because `mypy`/`ruff` both pass cleanly with a `TYPE_CHECKING`-only import — static
analysis has no way to know the symbol is unreachable at runtime. This is a real,
concrete example of exactly the failure mode the "zero stubs, zero lint errors"
metrics in every report so far cannot detect, because it's not a stub — it's a
perfectly clean, perfectly typed, runtime no-op.

**Open question this raises, not answered anywhere in the report:** was Item 7's Stage
D L2 evidence captured *before or after* `b4356cc`? The report doesn't give commit
ordering relative to the test run. If the L2 proof predates the fix, that specific
"PASS" needs to be re-run and re-reported, because it would have been technically
impossible for it to have passed as described.

**Ask:** confirm `git log --oneline` ordering of `b4356cc` relative to the Item 7
timestamp, or just re-run Item 7 fresh and attach a new timestamp+commit pair. This is
a five-minute check that resolves real uncertainty, not busywork.

---

## 3. Claims That Overstate Their Evidence

| # | Claim in report | What the evidence actually shows | Correction |
|---|---|---|---|
| C-01 | Item 6: "Concurrency safety — PASS (G-05, G-06)" | Politeness race test explicitly disclosed as asyncio tasks, not OS subprocesses (the report's own limitation note). PgBouncer search_path test doesn't state whether traffic actually routed through PgBouncer's transaction-pooling mode or hit Postgres directly. | G-06 is **partially** closed (proves Lua atomicity, doesn't prove OS-process-level scheduling behavior). G-05 needs one line of confirmation: was `PGBOUNCER_DSN` or the raw Postgres DSN used in that test's connection string? |
| C-02 | Item 7: "L2 live escalation — PASS" | The evidence is an ad-hoc script (Stage D), not a `pytest` run of the actual `test_l2_solves_standard_challenge` function that will execute in CI. Item 3's own scope note excludes `tests/live/` from the 168-test count. | The *capability* is proven; the *CI-integrated test* is not yet proven to pass as written. These are different claims — the report conflates them. |
| C-03 | Item 8: "L3 CPU-BOUND... Code structurally verified" | This diagnosis was wrong — see the fix in §4. It wasn't the VPS, it was an async-API-per-attempt design bug in the mirror. Framing a fixable design bug as an infrastructure ceiling would have led to either lowering the strict-tier difficulty (masking the real capability) or writing off L3 entirely as "can't be tested here" (worse). | Fixed and re-verified below — strict tier now solves in ~11.6s using real, executed JS. |
| C-04 | Item 4: worker.py coverage | Went from 59% (report 2) → 61% (report 3). This is the file the prior gap audit specifically flagged (G-03) as needing named tests per state-table row. A 2-point move on the single most important file in three rounds suggests that work hasn't actually happened yet, just that overall package percentage crossed the gate via improvements elsewhere (harvester 49%→75%, proxy pool). | Gate is met on paper; the highest-risk file specifically is still essentially where it was. |

---

## 4. Fixed, Not Just Flagged: The Strict-Tier PoW Timeout

I don't own your `worker.py` or `camoufox_wrapper.py`, so C-01/C-02/C-04 above are
asks, not something I can close myself. But the strict-tier timeout is in the
challenge-mirror I *do* own, so I fixed it instead of just noting it.

**Root cause:** `crypto.subtle.digest()` is unconditionally async — there is no
synchronous SubtleCrypto API. The original solver awaited one `digest()` call per PoW
attempt. At the strict tier's 5-hex-zero-nibble target (2^20 ≈ 1,048,576 expected
attempts), that's ~1M awaited microtasks. Per-call Promise/microtask scheduling
overhead, not hash throughput, is almost certainly what consumed the 60s budget in the
reported Camoufox run — "CPU-bound" was the wrong diagnosis.

**Fix:** a synchronous, from-scratch SHA-256 implementation, run in a tight loop with
a yield only every 200,000 attempts (to keep the tab responsive without reintroducing
per-attempt async overhead).

**Verification chain (each step actually executed, not asserted):**
1. Implemented the identical algorithm in Python first (`sha256-verification/verify_sha256.py`).
2. Checked it against `hashlib.sha256` across 10 cases including the classic 55/56/64-byte
   padding-boundary bugs. **All 10 matched exactly.**
3. Ported the verified algorithm to JS inside the mirror's `_challenge_html()`.
4. Did **not** just eyeball the JS — extracted the literal `<script>` block the live
   server emits and executed it in a real V8 context via Node's `vm` module
   (`tests/node_real_js_verify.js`), with a mocked `fetch` that round-trips to the
   actual running server.
5. Result: **standard tier ~1.5s, strict tier ~11.6s**, both including the real
   `/verify` HTTP round-trip against the live mirror. Previously: strict tier didn't
   finish inside 60s.
6. Re-ran the full 7-test pytest suite against the patched server — **7/7 still pass**,
   confirming the server-side verification logic (untouched) still agrees with the
   new client-side solver.

This directly un-blocks the "L3 CPU-BOUND" line in the summary matrix — re-run Item 7
Stage D against the patched mirror (`challenge-mirror/app/server.py`, updated) and the
strict-tier escalation-to-L3 test should now complete well within any reasonable
per-request timeout.

---

## 5. The Finding That Matters More Than Any CoveragectPercentage: BD-01

> "harvester.py (75%) = 4 proxy sources dead (BD-01 operational)"

Read plainly, this says: **all four default proxy sources
(`proxifly`/`proxyscrape`/`iplocate`/`proxripper`) are currently non-functional.**
Under the hard constraint "free proxies only, no paid residential proxies," this is
not a coverage gap — it's a statement that, *right now, with the current source list*,
the system has no way to populate a proxy pool at all. Every other metric in this
report (168 tests, 91.3% coverage, L2 Camoufox proof) describes a system that works
correctly *given a proxy* — none of it matters if `ProxyManager.get_proxy()` raises
`ProxyPoolExhaustedError` on every call because the pool is permanently empty.

This deserves to be the top line of the next report, not a parenthetical in a coverage
table. Concretely, before anything else:

1. **Verify independently** whether all four sources are actually dead (site-wide
   outage, API contract changed, or IP/rate-limit ban against the specific harvester
   host) versus a bug in `harvester.py`'s parsing of their current response format.
2. **Expand the source list.** proxybroker2's default set is not exhaustive; common
   additions in the free-proxy-list ecosystem include `free-proxy-list.net`,
   `geonode.com/free-proxy-list`, `spys.one`, and `openproxy.space` — each has
   different scrape/parse requirements and different uptime characteristics, and
   redundancy across sources is the only real mitigation for any single source going
   dark (which, per this report, just happened to all four at once — worth checking
   whether that's coincidence or a shared upstream dependency).
3. **Add a pool-health alert** (`ProxyPoolCriticallyLow`, already specified in
   blueprint v2 §9) wired to page someone the moment the pool crosses empty, since this
   is a silent, total-outage-class failure with no other visible symptom until a job
   fails.

I can't fix this one myself without your `harvester.py` and knowledge of which sources
you're willing to add — but it should be reprioritized above coverage-percentage work
on `worker.py` this round, since a perfectly-tested escalation ladder with no proxies
to escalate through doesn't ship.

---

## 6. Updated Gap Status

| Gap (from round 2) | Status now | Evidence |
|---|---|---|
| G-01 (no L2/L3 execution evidence) | **Substantially closed for L2**, reopened-then-fixed for L3 timeout | Item 7 Stage D (L2, pending commit-order confirmation per §2); strict-tier fix in §4 (needs a fresh Camoufox re-run to close fully) |
| G-02 (`browser/` unmeasured) | **Still open** — not in this report's coverage table either | — |
| G-03 (`worker.py` undertested) | **Still open** — 59%→61%, no named state-table tests evidenced | — |
| G-04 (`harvester.py` undertested) | **Improved** (49%→75%) but surfaced G-new (BD-01 dead sources) as a bigger problem | — |
| G-05 (PgBouncer + search_path race) | **Ambiguous** — test passed, routing through PgBouncer unconfirmed | needs one-line confirmation |
| G-06 (multi-process politeness race) | **Partially closed** — asyncio-task version only | still needs OS-subprocess version |
| G-07 (BD-05 self-contradiction) | **Closed** — mirror deployed, built, running, tested | Item 7 Stages A–C |
| G-08 (CapSolver live-solve) | **Still open**, honestly disclosed | "API key unavailable" |
| G-09 (Camoufox RSS assumption) | **Partially closed** — single-instance measured (80.1MB), 8-instance peak not measured | — |
| G-10 (coverage gate threshold) | **Improved** — 91.3% now exceeds the original 90% gate, no threshold-lowering needed this round | — |
| G-11 (SSRF redirect-chain test) | **Not mentioned this round** — status unknown | needs explicit re-check |
| **New: BD-01 proxy sources dead** | **Open, high severity** | §5 above |
| **New: Camoufox import runtime-dead until `b4356cc`** | **Fixed, but timing vs. Item 7 evidence unconfirmed** | §2 above |

---

## 7. What I'd Ask For Next, In Priority Order

1. **BD-01**: independent check of whether the 4 sources are really dead, plus a
   decision on which additional sources to add (§5).
2. **Commit-order confirmation** for `b4356cc` vs. the Item 7 L2 test timestamp (§2) —
   five minutes, resolves real uncertainty about whether the flagship result in this
   report is valid as reported.
3. **Re-run Item 7 Stage D against the patched mirror** to confirm the strict-tier
   fix actually closes the L3 escalation test end-to-end from your orchestrator's
   side, not just from my standalone Node verification.
4. Send `orchestrator/worker.py` directly — I'll write the named state-table tests
   from blueprint v2 §4.1 against your actual interfaces instead of asking you to.
5. Send `browser/camoufox_wrapper.py` post-`b4356cc` — I'll add it to a coverage
   check and look specifically for any other `TYPE_CHECKING`-only imports of runtime
   dependencies, since that bug class just proved it can hide behind clean lint/type
   output.
