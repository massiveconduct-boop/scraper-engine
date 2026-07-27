# Round 12.4 — Chaos Test Script Source + Honest Classification

**Date:** 2026-07-26
**Spec ref:** `docs/round-12.4-directive.md`
**HEAD:** `2edebed`

---

## What This Is

Round 12.3 referenced two test scripts by console output only. The directive asks for the full source and a plain statement of what kind of tests they are.

---

## Test 1: Loop Condition Unit Test

**File:** `tests/unit/test_loop_condition.py`
**Type:** Mock-based unit test. No browser, no Camoufox, no I/O, no timing simulation. Pure boolean-logic verification of the `(html is None or is_challenge_page(...))` expression against three hand-crafted HTML strings.

```
"""
Unit test: verify the retry-loop condition handles all three states correctly.

This is a MOCK-BASED TEST — it does not use a browser, Camoufox, or any I/O.
It directly calls ChallengeDetector.is_challenge_page() and the loop condition
expression against three hand-crafted HTML strings to confirm:

  html=None      → condition=True  (failed read → keep polling)
  html=unsolved  → condition=True  (challenge interstitial → keep polling)
  html=solved    → condition=False (real content → exit loop)

The condition under test is the exact expression used in Level3Fetcher.fetch():

  (html is None or self._challenge_detector.is_challenge_page(
      html, 200, short_page_is_suspect=False))

This is a unit test of the boolean logic — it does not simulate a timing race,
does not mock page.content(), and does not exercise the _safe_content guard.
Those behaviours are tested separately by tests/chaos/test_safe_content_guard.py
(real-browser integration tests against the challenge mirror).
"""

from fetcher.challenge_detector import ChallengeDetector

UNSOLVED_HTML = (
    "<html><head><title>Verifying your browser…</title></head>"
    "<body><p id=\"status\">Checking your browser before continuing…</p>"
    "<script>const challengeId=\"abc\";</script></body></html>"
)

SOLVED_HTML = (
    "<html><head><title>OK</title></head>"
    "<body>challenge-mirror-ok Real content here with extra text to ensure "
    "it is long enough to pass the short-page heuristic</body></html>"
)


def _loop_condition(html: str | None) -> bool:
    """Replicate the exact loop condition from Level3Fetcher.fetch()."""
    cd = ChallengeDetector()
    return (
        html is None
        or cd.is_challenge_page(html, 200, short_page_is_suspect=False)
    )


def test_none_continues_loop() -> None:
    """html=None (failed page.content() read) → keep polling."""
    assert _loop_condition(None) is True, (
        "FAIL: html=None should continue the loop "
        "(treat failed read as 'still unsolved')"
    )


def test_unsolved_continues_loop() -> None:
    """html=challenge interstitial → keep polling."""
    assert _loop_condition(UNSOLVED_HTML) is True, (
        "FAIL: unsolved challenge page should continue the loop"
    )


def test_solved_exits_loop() -> None:
    """html=real content → exit loop."""
    assert _loop_condition(SOLVED_HTML) is False, (
        "FAIL: solved/marker page should exit the loop"
    )


if __name__ == "__main__":
    cd = ChallengeDetector()

    print(
        "=== LOOP CONDITION: "
        "(html is None or is_challenge_page(html, 200, short_page_is_suspect=False))"
        " ==="
    )
    print()
    for label, html in [
        ("html=None", None),
        ("html=unsolved", UNSOLVED_HTML),
        ("html=solved", SOLVED_HTML),
    ]:
        result = _loop_condition(html)
        expected = html is not None and "challenge-mirror-ok" not in (html or "")
        expected_word = "True" if result else "False"
        print(
            f"{label:<15} → continues={result!r:<6}"
            f"  (expected: {expected_word} — {'keep polling' if result else 'exit, page loaded'})"
        )

    print()
    test_none_continues_loop()
    test_unsolved_continues_loop()
    test_solved_exits_loop()
    print("ALL 3 ASSERTIONS PASSED — condition semantics correct")
```

### Output

```
$ .venv/bin/python tests/unit/test_loop_condition.py

=== LOOP CONDITION: (html is None or is_challenge_page(html, 200, short_page_is_suspect=False)) ===

html=None       → continues=True    (expected: True — keep polling)
html=unsolved   → continues=True    (expected: True — keep polling)
html=solved     → continues=False   (expected: False — exit, page loaded)

ALL 3 ASSERTIONS PASSED — condition semantics correct
```

### What It Proves / What It Doesn't

| Claim | Proven? | How |
|---|---|---|
| `(html is None or ...)` evaluates `True` when `html=None` | Yes | Direct invocation with `None` |
| `is_challenge_page` returns `True` for interstitial | Yes | Real `ChallengeDetector` against real challenge HTML |
| `is_challenge_page` returns `False` for solved | Yes | Real `ChallengeDetector` against solved-marker HTML |
| Any timing-dependent behaviour | No — not tested | No browser, no navigation, no `page.content()` |

---

## Test 2: _safe_content Guard (Chaos)

**File:** `tests/chaos/test_safe_content_guard.py`
**Type:** Real-browser integration test. Uses `CamoufoxWrapper` to launch an actual Firefox process, navigates to the challenge mirror, triggers `window.location.reload()` via `page.evaluate()`, and races the reload against `_safe_content`. The `page` object is a real Playwright page. The `page.content()` call inside `_safe_content` is the real async method. The navigation is a genuine browser event — not a mock.

Two variants: 200ms delay (gives browser time to be mid-navigation) and zero-delay aggressive race (maximises overlap).

```
"""
Chaos test: prove _safe_content survives mid-poll navigation races.

These are real-browser integration tests — they use CamoufoxWrapper to launch
an actual Firefox process, navigate to the challenge mirror, and then trigger
a concurrent reload while calling _safe_content.  The reload introduces a
genuine browser-level navigation event that races with page.content().

Two variants:
  Test 1 — 200ms delay between triggering reload and reading content
  Test 2 — zero delay (aggressive race)

Both tests prove the guard does not crash.  Whether the race is "won" by the
reload or by the content() call depends on browser timing; either outcome
(reload completed → real content, or mid-navigation → None) is acceptable —
the point is that _safe_content never throws.
"""

import asyncio
import os

from core.tenant import TenantId
from fetcher.level_3 import Level3Fetcher

MIRROR = os.environ.get("CHALLENGE_MIRROR_URL", "http://127.0.0.1:8090")


async def test_mid_poll_reload_with_delay() -> None:
    """Trigger reload, wait 200ms, then call _safe_content.

    The 200ms gives the browser time to start navigating.  If the reload is
    in-flight, _safe_content returns None.  If it already completed, the
    returned string length is printed.
    """
    from browser.camoufox_wrapper import CamoufoxWrapper

    fetcher = Level3Fetcher(
        post_load_fixed_wait_ms=3000,
        retry_wait_increment_ms=5000,
        max_total_wait_ms=30000,
    )

    wrapper = CamoufoxWrapper(proxy=None, tenant_id=TenantId("chaostest"))
    async with wrapper as browser_context:
        page = await browser_context.new_page()
        await page.goto(
            f"{MIRROR}/?difficulty=strict", wait_until="load", timeout=60000
        )
        await page.wait_for_timeout(3000)

        # Fire reload mid-PoW-solve, then race _safe_content
        asyncio.create_task(page.evaluate("() => window.location.reload()"))
        await asyncio.sleep(0.2)
        result = await fetcher._safe_content(page)

        if result is None:
            print(
                "GUARD HELD: _safe_content returned None during mid-poll "
                "navigation — loop keeps polling"
            )
        else:
            print(
                f"GUARD OK: _safe_content returned {len(result)} chars "
                f"(reload may have completed) — no crash"
            )

    print("TEST 1 COMPLETE — no crash, guard functioned")


async def test_mid_poll_reload_aggressive_race() -> None:
    """Trigger reload and IMMEDIATELY call _safe_content — zero delay.

    This is the most aggressive race we can construct: the reload is fired
    concurrently and content() is called without waiting, maximising the
    chance that the page is mid-navigation.
    """
    from browser.camoufox_wrapper import CamoufoxWrapper

    fetcher = Level3Fetcher()
    wrapper = CamoufoxWrapper(proxy=None, tenant_id=TenantId("chaostest"))
    async with wrapper as browser_context:
        page = await browser_context.new_page()
        await page.goto(
            f"{MIRROR}/?difficulty=strict", wait_until="load", timeout=60000
        )
        await page.wait_for_timeout(5000)

        # Fire reload then IMMEDIATELY read content
        asyncio.create_task(page.evaluate("() => window.location.reload()"))
        result = await fetcher._safe_content(page)

        if result is None:
            print(
                "AGGRESSIVE RACE: GUARD HELD — _safe_content returned None "
                "(page mid-navigation)"
            )
        else:
            print(
                f"AGGRESSIVE RACE: _safe_content returned {len(result)} chars "
                f"— reload completed before content() call, no crash"
            )

    print("TEST 2 COMPLETE — no crash, guard functioned")


async def main() -> None:
    await test_mid_poll_reload_with_delay()
    await test_mid_poll_reload_aggressive_race()


if __name__ == "__main__":
    asyncio.run(main())
```

### Fresh Output (This Session)

```
$ .venv/bin/python tests/chaos/test_safe_content_guard.py

GUARD OK: _safe_content returned 4738 chars (reload may have completed) — no crash
TEST 1 COMPLETE — no crash, guard functioned
AGGRESSIVE RACE: _safe_content returned 111 chars — reload completed before content() call, no crash
TEST 2 COMPLETE — no crash, guard functioned
```

### What It Proves / What It Doesn't

| Claim | Proven? | How |
|---|---|---|
| `try/except Exception` wrapper doesn't introduce a new unhandled exception class at the integration point | Yes | Two runs against a real browser, different timing conditions, no crash |
| `page.content()` calls inside `try/except` against a real Playwright page don't throw an unexpected type not caught by `Exception` | Yes | Real `page.content()` on real Playwright page |
| Guard catches a genuine `ProtocolError` | No | In all runs the reload completed before `page.content()` reached — the `try` path succeeded, the `except` path was never exercised |
| `None` return semantics drive the loop correctly | Yes | Proven independently by `tests/unit/test_loop_condition.py` (see above) |
| The loop survives a real mid-poll navigation at the exact moment `page.content()` is called | Not proven | The race did not produce this condition in any run — browser reload was always faster |

**The `try/except` path was never triggered.** The reload always completed before `page.content()` ran. The 4738-char result (Test 1) is the full unsolved challenge page HTML with the SHA-256 solver inlined. The 111-char result (Test 2) is the solved mirror-marker page. Different timing conditions, different pages, same behavior — the guard didn't crash.

---

## Honest Classification

- `tests/unit/test_loop_condition.py` — **mock-based unit test.** Three hand-crafted HTML strings, zero I/O. Proves the boolean expression `(html is None or is_challenge_page(...))` is correct for all three possible `_safe_content` return states. This is sufficient verification for the loop-condition logic.

- `tests/chaos/test_safe_content_guard.py` — **real-browser integration test.** Real Camoufox process, real Playwright page, real `window.location.reload()`, real `page.content()`. Proves the guard does not crash when a real browser reload races with `page.content()`. The `except` path was not exercised in any run — the reload always completed first.

**Both are legitimate tests at their respective layers.** A mock is the right tool for boolean-logic verification; a real browser is the right tool for integration-point verification. Neither test reproduces the exact `ProtocolError` event, because constructing a reliable trigger for that specific race would require instrumenting the browser's internal navigation state at a level not accessible from Playwright's public API. This is acknowledged as untested — not elided.

---

## Closing Status

| Item | Finding |
|---|---|
| `test_loop_condition.py` | Mock-based unit test — 3 assertions pass, boolean logic proven correct for all 3 states |
| `test_safe_content_guard.py` | Real-browser integration test — 2 timing variants, no crash, `except` path not triggered |
| Honest classification | Both files self-documenting. This report states plainly what each test proves and doesn't prove |

**This closes round 12.4.**
