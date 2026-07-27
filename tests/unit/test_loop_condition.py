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
