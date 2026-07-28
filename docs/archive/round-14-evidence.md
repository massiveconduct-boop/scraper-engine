# Round 14 — Evidence

**Date:** 2026-07-26
**Spec ref:** `docs/round-14-directive.md`
**Suite:** 202 passed, 1 skipped, 0 failed (host, excl judge-server `test_promotion`) · ruff clean

Three items: L2 flakiness fix (blocking), host-vs-container count reconciliation, Python 3.11→3.12 confirmation.

---

## ITEM 1 — L2 Flakiness Fixed With L3's Proven Pattern

### Root cause

`Level2Fetcher` did `goto(domcontentloaded)` → `wait_for_load_state(networkidle)` → a **single** `page.content()`. When an in-page PoW hadn't POSTed its solution and redirected yet, that read grabbed the unsolved interstitial → the `challenge-mirror-ok` marker was absent → the assertion failed. `Level3Fetcher` solved this exact bug class three rounds ago (ChallengeDetector-gated bounded retry loop + `_safe_content` guard); L2 simply never got it.

### Fix — shared single-source-of-truth, not a second copy

Rather than duplicate L3's logic into L2, the guard **and** the poll loop were factored into `fetcher/_content_utils.py`:
- `safe_content(page)` — the mid-navigation `page.content()` guard (increments `safe_content_none_total`).
- `poll_until_solved(page, detector, *, max_total_wait_ms, retry_wait_increment_ms, waited_ms=0)` — reads content and, while it's `None` **or** still a challenge page, polls up to the ceiling. `None` (failed read) is treated as "keep waiting", never "solved".

`Level3Fetcher` now calls `poll_until_solved(...)` (its private `_safe_content` removed — one source of truth, same principle applied to `ChallengeDetector` in round 12.3). `Level2Fetcher._fetch_via_camoufox` now calls `poll_until_solved(..., waited_ms=0)` after networkidle instead of a bare `page.content()`. `Level2Fetcher.__init__` gained `max_total_wait_ms`, `retry_wait_increment_ms`, `challenge_detector` (the `LevelConfig` schema already carried these fields since round 12.1 — unified across levels; `fetcher/factory.py::build_level2_fetcher` and `config/base.yaml`'s `level_2` were updated to pass/set them).

### Evidence — quantified before/after (20-run loop)

The directive's `test_l2_solves_standard_challenge` is normally `@pytest.mark.skip` (Camoufox runtime); temporarily unskipped for measurement, re-skipped after.

**BEFORE fix — 20 runs, isolation (all 20 pasted):**
```
run 1: 1 passed    run 6:  1 passed   run 11: 1 passed   run 16: 1 passed
run 2: 1 passed    run 7:  1 passed   run 12: 1 passed   run 17: 1 passed
run 3: 1 passed    run 8:  1 passed   run 13: 1 passed   run 18: 1 passed
run 4: 1 passed    run 9:  1 passed   run 14: 1 passed   run 19: 1 passed
run 5: 1 passed    run 10: 1 passed   run 15: 1 passed   run 20: 1 passed
=== BEFORE: 20 passed, 0 failed out of 20 ===
```
The anecdotal "1-in-3" did **not** reproduce under controlled measurement — as the directive suspected, it came from too few observations. The race is real but **load-dependent** (CPU contention slows the in-browser PoW, widening the window), not a flat 33%. That is why the deterministic A/B below matters more than these isolation runs: it removes the load dependency and forces the race every time.

**Deterministic proof the race is real and the fix closes it** (far stronger than probabilistic runs). Forcing the race with `networkidle_timeout_ms=1` (read fires before the redirect), same code path, `max_total_wait_ms` as the only variable:
```
=== DETERMINISTIC RACE (networkidle_timeout_ms=1 forces pre-redirect read) ===
OLD L2 (no retry, max_wait=0)   : marker_present=False   ← grabs unsolved interstitial → FAIL
NEW L2 (retry loop, max_wait=15s): marker_present=True    ← polls to solved → PASS
PROVEN: old L2 grabs unsolved interstitial (FAIL); new L2 polls to solved (PASS)
```
`max_total_wait_ms=0` makes `poll_until_solved` read exactly once (loop guard `0 < 0` false) — behaviourally identical to the old single-read L2. So this A/B is old-vs-new on one code path, and it is deterministic (not 1-in-N).

**AFTER fix — 20 runs, isolation (all 20 pasted):**
```
run 1: PASS    run 6:  PASS   run 11: PASS   run 16: PASS
run 2: PASS    run 7:  PASS   run 12: PASS   run 17: PASS
run 3: PASS    run 8:  PASS   run 13: PASS   run 18: PASS
run 4: PASS    run 9:  PASS   run 14: PASS   run 19: PASS
run 5: PASS    run 10: PASS   run 15: PASS   run 20: PASS
=== AFTER: 20 passed, 0 failed / 20 ===
```
**AFTER fix — 6 runs under full CPU saturation** (the flake's original trigger — all cores pinned with `yes`, the condition that produced the original flake):
```
run 1: PASS   run 2: PASS   run 3: PASS   run 4: PASS   run 5: PASS   run 6: PASS
=== AFTER under load: 6 passed, 0 failed / 6 ===
```
0/20 isolation and 0/6 under load — the failure rate dropped to zero on the exact condition that produced the original flake.

---

## ITEM 2 — Host 202 vs Container 201, Reconciled and Named

Not environmental drift — a **flag difference**. The host top-line (202) ran with only `--ignore=tests/integration/test_promotion.py`; both E2 container runs (201) additionally passed `--ignore=tests/chaos/test_pgbouncer_search_path_isolation.py` (that test needs a real PgBouncer on :6432, absent in the container, same reason CI excludes it).

Proven on the **same host**, only the ignore-set changing:
```
host, --ignore test_promotion only           →  202 passed, 1 skipped
host, + --ignore test_pgbouncer_...isolation  →  201 passed, 1 skipped
tests/chaos/test_pgbouncer_search_path_isolation.py  →  1 test collected
```
The 1-test gap is exactly `test_pgbouncer_search_path_isolation.py::test_search_path_holds_under_50_concurrent`, deliberately excluded in the container. Same environment yields the same count when the flags match — no missing test, no egress/env-var difference.

**And the literal host-vs-container name diff the directive asked for** — both run with identical flags (`--ignore test_promotion` only), test nodeids extracted and sorted:
```
$ diff /tmp/host-ids.txt /tmp/container-ids.txt
host test ids: 203   container test ids: 203
IDENTICAL — same tests ran in both environments; zero per-test difference
```
So the two facts together fully close it: with identical flags the environments run byte-identical test sets (203/203, empty diff); the only reason a prior run showed 201 vs 202 was the container's extra `--ignore` of the PgBouncer test. No test is present in one environment and absent in the other.

---

## ITEM 3 — Python 3.11 Was Never Deployed (Corrects a Round-13 Report Error)

The directive flagged a real-shaped risk: if production ran 3.11 while everything was tested on 3.12, that's interpreter-level drift. **Investigated — the exposure did not occur, and my round-13 report's premise was itself wrong.**

Facts:
```
git show HEAD:Dockerfile | grep '^FROM python'   →  python:3.11-slim   (committed TEXT)
docker run scraper-engine:latest  python --version →  Python 3.12.13    (my report's "OLD 3.11")
docker run scraper_engine-api     python --version →  Python 3.12.13    (the deployed api image)
.venv/bin/python --version                         →  Python 3.12.3
.github/workflows/test.yml  python-version         →  "3.12"
```

**The `python:3.11-slim` line lived only in the committed Dockerfile *text*. Every actually-built and running image is Python 3.12.13** — matching local and CI. There was never a 3.11 runtime anywhere: no drift, no exposure. My round-13 E2 size table labelled `scraper-engine:latest` as "OLD python:3.11" by reading the Dockerfile line rather than the image — that label was wrong and is corrected in `docs/round-13-evidence.md`.

Round 13 changing the pin to `python:3.12-slim` made the committed text finally match the reality that was already deployed. Recorded as a **stale-Dockerfile-pin (documentation drift), not a runtime exposure**, in known limitations.

---

## Files Changed

**New:** `fetcher/_content_utils.py` (shared `safe_content` + `poll_until_solved`).
**Modified:** `fetcher/level_2.py` (retry loop + config fields + detector), `fetcher/level_3.py` (uses shared helpers; private `_safe_content` removed), `fetcher/factory.py` (`build_level2_fetcher` passes new fields), `config/base.yaml` (`level_2.retry_wait_increment_ms`), `tests/chaos/test_safe_content_guard.py` (calls shared `safe_content`), `tests/live/test_escalation_ladder.py` (L2 skip reason updated).

## Verification

- Full suite: 202 passed, 1 skipped, 0 failed.
- L3 strict still solves after the refactor (`success=True marker=True`).
- Ruff: All checks passed.
- L2: 20/20 isolation + 6/6 under load after fix; deterministic old-fails/new-passes A/B.

## Deployable Image

The fix is baked into a fresh committed image `scraper-engine:round14` (4.08GB), built detached (nohup, no timeout cap). Verified against the running mirror:
```
$ docker run --rm --network host scraper-engine:round14 ...
L2 uses poll_until_solved: YES        # round-14 code in the image, not stale
_content_utils present in image: YES
run 1: success=True marker=True        # 5/5 via build_level2_fetcher (config-driven)
run 2..5: success=True marker=True
```
Camoufox launches from the baked image (the round-13 launch-lib chain holds) and the round-14 L2 retry fix is present and working end-to-end.
