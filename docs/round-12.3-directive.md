# Round 12.3 — Two Specific Issues in the New Retry Loop

The config-wiring proof is exactly right — a live demonstration of values flowing
`production.yaml` → `AppConfig` → constructor → instance field → wait logic is
the correct way to prove "load-bearing," not just an assertion. Accepted. Two
things in `Level3Fetcher`'s new bounded retry loop need attention.

---

## 1 — `_page_looks_unsolved` Duplicates `ChallengeDetector`, Doesn't Use It

Round 11's request was specific: replace the hardcoded `"challenge-mirror-ok"`
success-literal with either config or "the real `ChallengeDetector` (per
blueprint §3.9)... making this determination against real targets." What shipped
is a *different* hardcoded literal set instead of the first one:

```python
_UNSOLVED_CHALLENGE_PATTERNS: tuple[str, ...] = (
    "<title>Verifying your browser",
    "Just a moment...",
)
```

This is the same shortcut wearing a slightly larger hat. The project already has
a `ChallengeDetector` component — `FetchResult.is_challenge_page` is populated by
it, `storage/dedup.py`'s success-gating depends on it, and it's presumably the
thing that's supposed to know what a Cloudflare/DataDome/PerimeterX interstitial
looks like across real targets, not just two literal strings. Now there are
**two independent places** that need to agree on "does this page look like an
unsolved challenge" — the real `ChallengeDetector` and this private 2-item tuple
inside `Level3Fetcher` — and they will drift, because nothing keeps them in
sync. If `ChallengeDetector` ever gets a third pattern added (a new anti-bot
vendor, an updated Cloudflare interstitial string), this retry loop won't know
about it unless someone remembers to update both places.

**Required:** call the actual `ChallengeDetector` from inside the retry loop
instead of maintaining a parallel pattern list:
```python
html = await page.content()
detection = self._challenge_detector.classify(html)  # or whatever its real method signature is
while (
    html is not None
    and detection.is_challenge_page
    and waited_ms < self._max_total_wait_ms
):
    await page.wait_for_timeout(self._retry_wait_increment_ms)
    waited_ms += self._retry_wait_increment_ms
    html = await page.content()
    detection = self._challenge_detector.classify(html)
```
If `ChallengeDetector`'s real interface doesn't currently expose something
callable synchronously mid-fetch this way, that's the actual gap to close —
adapt `ChallengeDetector`'s constructor/method so `Level3Fetcher` can use it
directly, rather than reimplementing a shadow copy of its job. If there's a
concrete reason the two must stay separate (e.g., `ChallengeDetector` needs
response headers/status that aren't available mid-poll, only full HTML), state
that reason explicitly — but don't leave two silently-diverging implementations
of the same classification without addressing which one is authoritative.

---

## 2 — The Retry Loop's Own `page.content()` Calls Are Unguarded — Risk of Reintroducing the Original Bug

The entire reason this multi-round effort exists is:
`Page.content: Unable to retrieve content because the page is navigating and
changing the content` — thrown when `page.content()` is called while the page is
mid-transition. `Level2Fetcher`'s fix wraps its `wait_for_load_state` call in
`contextlib.suppress(Exception)` specifically to guard against this class of
timing issue. `Level3Fetcher`'s new retry loop calls `page.content()` **twice**,
raw, with no guard:
```python
html = await page.content()          # before the loop
...
html = await page.content()          # inside the loop, every iteration
```
If the strict-tier challenge's JS triggers any navigation or DOM replacement
between polls (plausible — some anti-bot challenges redirect or reload once
solved), one of these calls can throw the exact original error, uncaught, inside
a loop that's specifically there to wait out slow challenges. That would
reproduce the original `BROWSER_CRASH` failure mode at a new call site, on
exactly the class of target (slow, JS-heavy) this retry loop was built to
handle.

**Required:**
```python
async def _safe_content(page) -> str | None:
    try:
        return await page.content()
    except Exception:
        return None  # treat as "still unsolved, keep polling" rather than crashing

html = await _safe_content(page)
...
while (html is None or detection.is_challenge_page) and waited_ms < self._max_total_wait_ms:
    await page.wait_for_timeout(self._retry_wait_increment_ms)
    waited_ms += self._retry_wait_increment_ms
    html = await _safe_content(page)
    detection = self._challenge_detector.classify(html) if html else detection
```
Note the loop condition also needs to treat `html is None` (a failed read) as
"keep waiting," not as "solved" — the current code's `html is not None and
self._page_looks_unsolved(html)` would silently exit the loop and report success
if `page.content()` ever returned `None` from a caught exception, which is the
opposite of what should happen.

**Required evidence:** re-run the L2/L3 live tests after both fixes, same
command as before, paste fresh timings — and if possible, a chaos-style test
that forces a mid-poll navigation event (even a crude one: trigger a client-side
`window.location.reload()` partway through the mirror's PoW solve) to prove the
guard actually holds under the condition it's meant to survive, not just under
the mirror's normal, well-behaved timing.

---

## Not Reopening

Config wiring (Item 1 from 12.2) is genuinely done — the load-bearing
demonstration is solid evidence and doesn't need to be redone. This directive is
scoped to the two issues above, both inside `Level3Fetcher`'s new retry loop
specifically.

## Closing Condition

If the `ChallengeDetector` integration replaces the private pattern tuple (or a
clear, argued reason is given for why it can't), and the `page.content()` calls
inside the retry loop are exception-guarded with fresh passing evidence — **this
closes the project.** No further items pending beyond these two.
