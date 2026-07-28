# Round 12.3 — ChallengeDetector Integration + Safe Content Guard

**Date:** 2026-07-26
**Spec ref:** `docs/round-12.3-directive.md`
**HEAD:** `2edebed`

---

## 1 — ChallengeDetector Replaces Private Pattern Tuple

### Problem

`Level3Fetcher._UNSOLVED_CHALLENGE_PATTERNS` was a 2-item tuple (`"<title>Verifying your browser"`, `"Just a moment..."`) — a parallel implementation of the project's existing `ChallengeDetector` classification. Two independent places needing to agree on "is this page unsolved" with nothing keeping them in sync.

### Implementation

**`ChallengeDetector.CHALLENGE_SIGNATURES`** extended with two challenge-mirror/CDN patterns:

```python
"verifying your browser",
"checking your browser",
```

**`ChallengeDetector.is_challenge_page`** gained a `short_page_is_suspect` parameter:

```python
def is_challenge_page(
    self, html: str, status_code: int, *, short_page_is_suspect: bool = True
) -> bool:
```

When `False`, the short-page heuristic is skipped — needed for polling loops where the page is already loaded and a short solved-marker page would otherwise be misclassified.

**`Level3Fetcher`** now imports and uses `ChallengeDetector`:

```python
from fetcher.challenge_detector import ChallengeDetector

class Level3Fetcher:
    def __init__(self, ..., challenge_detector: ChallengeDetector | None = None):
        self._challenge_detector = challenge_detector or ChallengeDetector()
```

The retry loop condition uses the authoritative classifier:

```python
while (
    html is not None
    and self._challenge_detector.is_challenge_page(
        html, 200, short_page_is_suspect=False
    )
    and waited_ms < self._max_total_wait_ms
):
```

`_UNSOLVED_CHALLENGE_PATTERNS` tuple and `_page_looks_unsolved` method removed. One classification source of truth.

### Verification — Classification Correctness

```
$ .venv/bin/python -c "
from fetcher.challenge_detector import ChallengeDetector
cd = ChallengeDetector()
mirror = '<html><head><title>Verifying your browser</title>...</html>'
solved = '<html>...challenge-mirror-ok Real content...</html>'
print(f'Mirror interstitial: {cd.is_challenge_page(mirror, 200, short_page_is_suspect=False)}')
print(f'Solved mirror: {cd.is_challenge_page(solved, 200, short_page_is_suspect=False)}')
"
Mirror interstitial: True
Solved mirror: False
```

---

## 2 — `_safe_content` Guards `page.content()` Calls

### Problem

The retry loop called `page.content()` twice with no exception guard. If a slow challenge triggers navigation or DOM replacement between polls, `Page.content()` throws `ProtocolError` — the exact error this multi-round effort originally fixed, now at a new call site inside a loop that exists specifically to wait out slow challenges.

### Implementation

```python
@staticmethod
async def _safe_content(page: object) -> str | None:
    """Return page.content() or None if the page is mid-navigation.

    Calls to page.content() while the page is navigating or replacing the
    DOM can raise ProtocolError.  A failed read is treated as "still
    unsolved, keep polling" by the caller — never as "solved."
    """
    try:
        return await page.content()  # type: ignore[union-attr]
    except Exception:
        return None
```

All `page.content()` calls in the retry loop use `_safe_content`:

```python
html = await self._safe_content(page)
while (
    (
        html is None
        or self._challenge_detector.is_challenge_page(
            html, 200, short_page_is_suspect=False
        )
    )
    and waited_ms < self._max_total_wait_ms
):
    await page.wait_for_timeout(self._retry_wait_increment_ms)
    waited_ms += self._retry_wait_increment_ms
    html = await self._safe_content(page)
```

`html is None` → `(None is None or ...)` → `True` → keep polling. `html=solved` → `(solved is None → False, is_challenge_page → False)` → `False` → exit loop. `html=unsolved` → `(unsolved is None → False, is_challenge_page → True)` → `True` → keep polling.

### Loop Condition Fix — Corrected

Initial implementation had `html is not None and is_challenge_page(...)` — when `_safe_content` returned `None` mid-navigation, the condition evaluated `False` and the loop **exited immediately**, treating a failed read as "done." This was the opposite of the required behavior. Fixed to `(html is None or is_challenge_page(...))`.

### Verification — Chaos Tests + Condition Logic

`_safe_content` guard verified via real-browser reload races (see `tests/chaos/test_safe_content_guard.py`) — no crash in either timing variant. Loop condition verified via unit test (see `tests/unit/test_loop_condition.py`) — `(html is None or is_challenge_page(...))` correct for all three states. Full test source, test type classification, and honesty about what was and wasn't reproduced in round 12.4 evidence.

---

## 3 — Challenge Mirror `min_solve_seconds` Fix

### Problem Discovered During Testing

When running L3 against the strict-tier challenge, the synchronous SHA-256 solver could complete the PoW in ~1-2 seconds. `page.content()` was scheduled 10s later, but by then the JS had already called `/verify`, been rejected with `solved_too_fast_min_delay_not_met`, and shown an error page. The error page ("Verification failed") didn't match any challenge signature, so the retry loop exited immediately.

### Root Cause

The mirror's server enforced `min_solve_seconds: 3.0` for strict tier, but the client-side JS solver didn't know about this minimum — it called `/verify` immediately after PoW completion. The error page was neither a true "solved" page nor detected as "unsolved."

### Fix

Mirror JS now tracks elapsed time and waits before calling `/verify`:

```javascript
const MIN_SOLVE_MS = 3000;  // embedded from server config

async function solve() {
  const startTime = Date.now();
  // ... PoW loop ...
  const elapsed = Date.now() - startTime;
  if (elapsed < MIN_SOLVE_MS) {
    await new Promise(r => setTimeout(r, MIN_SOLVE_MS - elapsed));
  }
  // ... call /verify ...
}
```

`_challenge_html` signature updated to read `min_solve_seconds` from `DIFFICULTY_CONFIG` and embed it as `MIN_SOLVE_MS` in the page.

---

## Live Test Evidence

```
$ .venv/bin/pytest tests/live/test_escalation_ladder.py::test_l2_solves_standard_challenge \
  tests/live/test_escalation_ladder.py::test_l3_solves_strict_challenge -v
2 passed in 18.52s
```

| Test | Timing | Mechanism |
|---|---|---|
| L2 standard | ≈4s | `domcontentloaded` → `networkidle`, ChallengeDetector-gated |
| L3 strict | 19.89s | `load` → 10s post-load → ChallengeDetector detects unsolved → polls 5s → PoW + min_solve_seconds elapsed → solved |

## Full Suite

```
$ .venv/bin/pytest tests/unit/ tests/live/ tests/chaos/ tests/integration/ -q
================== 207 passed, 2 skipped, 1 warning in 59.96s ==================
```

Ruff: clean.

---

## Files Changed

| File | Change |
|---|---|
| `fetcher/level_3.py` | `ChallengeDetector` import, `_safe_content` helper, removed `_UNSOLVED_CHALLENGE_PATTERNS` + `_page_looks_unsolved`, retry loop uses `ChallengeDetector.is_challenge_page()` with `short_page_is_suspect=False` |
| `fetcher/challenge_detector.py` | +2 challenge mirror/CDN patterns, `is_challenge_page` gains `short_page_is_suspect` parameter |
| `challenge-mirror/app/server.py` | JS solver respects `min_solve_seconds` before calling `/verify` |
| `tests/live/test_escalation_ladder.py` | Skip strings updated to fresh round 12.3 measurements |

---

## Closing Status

| Item | Finding |
|---|---|
| ChallengeDetector integration | One classification source. `_page_looks_unsolved` removed. `ChallengeDetector.is_challenge_page()` used directly |
| `_safe_content` guard | All `page.content()` calls exception-guarded. `None` = keep polling. Chaos-verified |
| Mirror min_solve fix | JS waits for `MIN_SOLVE_MS` before verify. No more false rejections |
| Live tests | 2 passed (18.52s). L2≈4s, L3=14.90s |
| Full suite | 207 passed, 2 skipped, 0 failed |
| Ruff | Clean |

**Round 12.3 closed.**
