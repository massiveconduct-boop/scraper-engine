# Round 15 — Real-Target Validation Against Public Test/Anti-Bot Sandboxes

**Date:** 2026-07-26
**Scope:** The round-13 Part C real-target validation, now run against operator-provided public test targets (all purpose-built for scraping/anti-bot testing). Exploratory by design — the job was to find where the scraper's real behaviour matches or diverges from design, not to assume it passes.
**Harness:** `tools/validate_real_sites.py` (L1/L2/L3 via the config-driven factory).

---

## Results — all 7 targets, all three levels where relevant

| Target | Level | success | detector | content marker | verdict |
|---|---|---|---|---|---|
| books.toscrape.com | L1 | True (200) | clean | FOUND ("A Light in the Attic") | ✅ clean extraction, 364ms |
| quotes.toscrape.com | L1 | True (200) | clean | FOUND ("Albert Einstein") | ✅ clean, 217ms |
| scrapethissite.com | L1 | True (200) | clean | FOUND ("Andorra") | ✅ clean, 736ms |
| webscraper.io e-commerce | L1 | True (200) | clean | MISSING | ⚠️ JS-rendered — L1 gets the shell (see below) |
| webscraper.io e-commerce | L2/L3 | True (200) | clean | (real products) | ✅ JS renders — see marker note |
| webscraper.io scroll | L2/L3 | True (200) | clean | (real products) | ✅ renders |
| **nowsecure.nl** (Cloudflare) | L2/L3 | True (200) | clean | — | ✅ **Cloudflare PASSED** |
| **bot.sannysoft.com** (fingerprint) | L2/L3 | True (200) | clean | FOUND | ✅ **no webdriver leak** |
| harvester.scrapecups.me | L2/L3 | False | no-html | — | ⚠️ **domain does not resolve** (not a scraper defect) |

---

## What the anti-bot targets actually revealed

### nowsecure.nl — Camoufox defeats real Cloudflare
`<title>` is `nowsecure.nl` (the real site), **not** "Just a moment…". None of the Cloudflare interstitial strings are present (`Just a moment`, `Verifying you are human`, `cf-challenge`, `Attention Required`). 179 KB of real content returned. The only reason the harness marked the marker "MISSING" is that my expected string "You are through" is **outdated** — nowsecure changed its success text. Verdict: **Camoufox stealth passes real Cloudflare bot-fight mode.**

### bot.sannysoft.com — no navigator.webdriver leak
Precise per-row extraction of the fingerprint table:
```
WebDriver (New):       missing (passed)
WebDriver Advanced:    passed
```
Both webdriver checks pass — Camoufox does **not** leak `navigator.webdriver=true`. (Correction: an earlier coarse regex in my investigation matched the page's *legend* text "present (failed)" and I momentarily read it as a leak — the precise per-`<tr>` extraction above is the real result, and it's clean. Consistent with passing Cloudflare.)

### webscraper.io — JS content renders; my marker string was wrong
L1 returned 200 with 24 KB (the un-rendered shell). L2/L3 returned 72–76 KB, and the actual rendered content contains `Computers`, `Phones`, `product-wrapper`, `MSI` — real product data. The "Laptops" marker I picked simply isn't the category label the site uses. **Not a scraper failure — a wrong assertion string on my side.**

### harvester.scrapecups.me — dead domain, correctly handled
Raw curl: `CURLE_COULDNT_RESOLVE_HOST`, http=000, 0.001s. Camoufox: `Page.goto: NS_ERROR_UNKNOWN_HOST`. The domain does not resolve from this environment — there is nothing to scrape. The scraper returned `success=False` without hanging. Not a stealth or scraper defect.

---

## Real gap found and fixed at root cause — DNS failures were retryable

The scrapecups dead-domain case exposed a genuine bug: an unresolvable host was categorised as `BROWSER_CRASH`, which the retry matrix marks **retryable (2 attempts)**. So a domain that can never resolve would be retried per level **and** escalated L1→L2→L3 — up to 6 wasted browser launches on a host that will never answer (observed: L2 and L3 each spent ~2.8s crashing on it).

**Fix (root cause, not a patch):**
- New `FailureCategory.HOST_UNREACHABLE` (`core/models.py`), **non-retryable** in the retry matrix (`core/retry.py`, `max_attempts=0, retryable=False`).
- New shared classifier `fetcher/_failure.py::classify_fetch_exception(exc, default)` — maps DNS/unknown-host markers (`NS_ERROR_UNKNOWN_HOST`, `getaddrinfo`, `Name or service not known`, `Could not resolve host`, …) to `HOST_UNREACHABLE`, everything else to the caller's default.
- Wired into all three fetchers' except blocks (L1 default `NETWORK_TIMEOUT`, L2/L3 default `BROWSER_CRASH`).

**Verified:**
```
categories missing from matrix: none
HOST_UNREACHABLE retryable: False
NS_ERROR_UNKNOWN_HOST -> host_unreachable   generic crash -> browser_crash
scrapecups category: host_unreachable        # was browser_crash (retryable) before
```
A dead domain now dead-letters immediately instead of escalating through all three levels.

---

## Second finding — L1 "200 but under-rendered" — FIXED at root

`webscraper.io e-commerce` returned L1 `success=True, detector=clean` with only the JS shell. The escalation ladder escalated only on *failure/challenge*, so a JS-only SPA that returns 200 with an empty mount point would be **accepted and cached as a shell** — real content never fetched.

**Fix (root cause):**
- `ChallengeDetector.looks_javascript_gated(html)` — conservative JS-gated-shell detector. Fires only when a JS-required marker (`you need to enable javascript`, …) **or** an empty SPA mount (`<div id="root"></div>`, `<div id="app"></div>`, `<app-root>`, `<div id="__next">`) is present **AND** the visible text is thin (<500 chars). A fully-rendered static page that merely carries a `<noscript>` analytics tag has plenty of visible text and does **not** trip it.
- `orchestrator/worker.py`: when a **non-final** level returns `success=True` but `looks_javascript_gated`, the worker no longer accepts it — it escalates to the next (browser) level that runs JS. Browser levels render JS so they don't trip this; the final level accepts whatever it got.

**Verified** (`tests/unit/test_challenge_detector.py`, `tests/unit/test_worker.py::test_js_gated_l1_escalates`):
```
empty SPA shell      -> True   (escalate)
noscript+empty root  -> True   (escalate)
full static+noscript -> False  (no false escalation)
normal content       -> False
worker: L1 shell → escalates → accepts L2 render (level_used == 2)
```

## Third finding — HOST_UNREACHABLE was set but the worker ignored it — FIXED

Round-15's DNS category was correct, but the worker's escalation loop only dead-lettered a hardcoded 3-category set (`SSRF_BLOCKED`, `QUOTA_EXCEEDED`, `PROXY_EXHAUSTED`) — so a `HOST_UNREACHABLE` failure at L1 still fell through and escalated to L2/L3, wasting the very browser launches the category was meant to prevent. Added `HOST_UNREACHABLE` to that set: a dead/unresolvable host now dead-letters immediately at L1 (a browser can't resolve DNS the HTTP client couldn't). Verified: `test_host_unreachable_dead_letters_without_escalation` asserts `_fetch_url.await_count == 1` (no L2/L3 attempts).

---

## Production-Readiness Verdict — honest

**Strong.** Against real, independent targets:
- **L1** extracts static content cleanly and fast (3/3 static markers, sub-second).
- **L2/L3** render real JS e-commerce content (webscraper.io products present).
- **Stealth holds against real anti-bot:** Camoufox passes nowsecure.nl's Cloudflare challenge and leaks no `navigator.webdriver` on sannysoft — the two hardest targets in the set.
- **Failure handling is graceful:** an unresolvable domain returns a clean failure, and (after this round's fix) is correctly categorised non-retryable so it doesn't waste the escalation ladder.

The L1 under-render gap that was "noted, not blocking" in the first draft is now **fixed at root** (JS-gated escalation), as is the incomplete HOST_UNREACHABLE wiring. One honest caveat remains: one target in the set (scrapecups) was simply an unreachable domain so its stealth couldn't be assessed. Everything that *could* be tested passed, including the two genuine anti-bot benchmarks.

Self-corrections made in the open: the "Laptops"/"You are through" markers were wrong strings (not scraper failures), and my first coarse read of the sannysoft webdriver cell was a legend false-positive — the precise result is clean.

---

## Files Changed

**New:** `tools/validate_real_sites.py` (validation harness), `fetcher/_failure.py` (DNS/host classifier), `tests/unit/test_challenge_detector.py` (detector + JS-gated coverage).
**Modified:** `core/models.py` (`HOST_UNREACHABLE`), `core/retry.py` (non-retryable strategy), `fetcher/level_1.py` / `level_2.py` / `level_3.py` (classify exceptions), `fetcher/challenge_detector.py` (`looks_javascript_gated`), `orchestrator/worker.py` (JS-gated escalation + HOST_UNREACHABLE dead-letter), `tests/unit/test_worker.py` (2 regression tests).

## Verification
- Full suite: **213 passed, 1 skipped, 0 failed** (11 new: 9 detector + 2 worker). Ruff clean. Retry matrix covers all categories. Both CI grep-gates pass.
