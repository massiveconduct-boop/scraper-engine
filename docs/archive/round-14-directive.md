# Round 14 — The L2 Flakiness Is the Actual Finding Here

Strong round overall — the Docker launch-chain bugs are a legitimate, valuable
catch, and the gate-proving discipline (deliberately violating each new CI check
once, watching it fire, reverting) continues to be applied correctly. But one
sentence in B1's evidence deserves to be the lead item of the next round, not a
footnote:

> "It flaked 1-in-3 on the marker (networkidle fired before the PoW POST/redirect
> completed) — a pre-existing L2 property (L2 has no ChallengeDetector-gated
> retry loop like L3)."

---

## ITEM 1 — Fix L2's Flakiness With the Pattern Already Proven for L3 — BLOCKING

Read plainly: **Level 2, the middle tier of the escalation ladder, has an
approximately 33% failure rate on real challenge content** — not from
detection, not from proxy quality, but from a pure timing race that `Level3Fetcher`
already solved three rounds ago (`ChallengeDetector`-gated bounded retry loop,
`_safe_content` exception guard). That fix exists, is tested, is proven. L2
simply doesn't have it. This is not a new problem to design — it's a known,
working solution that hasn't been applied to the second place it's needed.

**Required fix — apply the identical pattern, not a new one:**
```python
# fetcher/level_2.py
async def fetch(self, url: str, *, proxy=None, tenant_id=None, domain=None):
    if self._force_engine == "raw_playwright":
        return await self._fetch_via_raw_playwright(url, proxy)
    return await self._fetch_via_camoufox(url, proxy, tenant_id, domain)

async def _fetch_via_camoufox(self, url, proxy, tenant_id, domain):
    page = await browser_context.new_page()
    await page.goto(url, wait_until=self._goto_wait_until, timeout=self._timeout_ms)
    with contextlib.suppress(Exception):
        await page.wait_for_load_state("networkidle", timeout=self._networkidle_timeout_ms)

    html = await self._safe_content(page)   # reuse Level3Fetcher's guard — same bug class
    waited_ms = 0
    while (
        (html is None or self._challenge_detector.is_challenge_page(html, 200, short_page_is_suspect=False))
        and waited_ms < self._max_total_wait_ms
    ):
        await page.wait_for_timeout(self._retry_wait_increment_ms)
        waited_ms += self._retry_wait_increment_ms
        html = await self._safe_content(page)
    return html
```
This means `Level2Fetcher` needs the same config fields `Level3Fetcher` already
has (`max_total_wait_ms`, `retry_wait_increment_ms`, a `ChallengeDetector`
instance) — extend `config/schema.py`'s `level_2` block to match `level_3`'s
shape (it's currently missing `max_total_wait_ms`/`retry_wait_increment_ms`
entirely per round 12.2's schema), and update `fetcher/factory.py::build_level2_fetcher`
accordingly. Consider factoring `_safe_content` out of `Level3Fetcher` into a
shared module (`fetcher/_content_utils.py` or similar) rather than duplicating
it a second time now that two fetchers need it — same "one source of truth"
principle already applied to `ChallengeDetector` in round 12.3.

**Required evidence — quantified, not anecdotal:**
"1-in-3" reads like it came from roughly 3 observed runs, which isn't enough to
characterize a race condition's real failure rate. Before the fix:
```bash
for i in $(seq 1 20); do
  .venv/bin/pytest tests/live/test_escalation_ladder.py::test_l2_solves_standard_challenge -v 2>&1 | tail -1
done
```
Paste all 20 results, count the actual failure rate. After the fix, repeat the
same 20-run loop and paste all 20 results — the fix is proven only if the
failure rate drops to 0/20 (or is at least dramatically reduced with a clear
explanation of any remaining flakiness), not just "it passed once."

---

## ITEM 2 — Reconcile Host (202 passed) vs In-Container (201 passed)

The report's own top-line summary states 202 passed on the host. Both
in-container runs (E2) report 201 passed. That's a real, unexplained 1-test
gap between two environments in the same report — small this time, but this
project has already learned once that an unreconciled count difference, however
small, turned out to be a real bug (round 6) and once turned out to be real
data loss (round 12). Don't let a 1-test gap go unnamed just because it's small.

**Required:** run both suites back-to-back in the same session and diff the
actual test names, not just the totals:
```bash
.venv/bin/pytest tests/unit/ tests/integration/ tests/chaos/ -v --tb=no | grep -E "PASSED|SKIPPED|FAILED" > /tmp/host-results.txt
docker run --rm --network host scraper-engine:round13 sh -c '... pytest ... -v --tb=no' > /tmp/container-results.txt
diff /tmp/host-results.txt /tmp/container-results.txt
```
Paste the diff. Name the specific test that's present in one environment and
not the other, and say why (network egress difference, an environment variable
only set on the host, a test that's host-OS-specific).

---

## ITEM 3 — Confirm and Name the Python 3.11→3.12 Docker Base Image Change

E2's before/after table lists "OLD (round12, python:3.11)" vs "NEW (round13,
python:3.12)" as a side note inside a size comparison. If this is accurate, it
means **the production Docker image was running Python 3.11 this entire time**
while every local and CI test in this project's history — every `pip show`,
every `.venv/bin/pytest` — ran against **3.12**. That's a real, previously
unflagged environment-drift risk of exactly the same shape as the `asyncpg`/
`httpx` version-drift issue from several rounds back, just at the interpreter
level instead of the dependency level, and potentially more consequential
(3.11→3.12 changes real language/stdlib behavior, not just library APIs).

**Required:** confirm explicitly — was `python:3.11-slim` genuinely the base
image before this round, and if so, was that image ever actually deployed
anywhere, or did deployment only ever happen from a locally-built (3.12) image?
If the 3.11 image was ever live, note this plainly in the project's known-
limitations record as a historical exposure that's now closed, not just an
implicit fact recoverable from a table row.

---

## Not Reopened

A1 (config DI + gate), D1/D2 (dashboards, alerts, per-source health), C (the
real-target scaffold, correctly not run without owned infrastructure), A2/A3
(ruff), E1 (mypy shrinkage advisory) — all accepted as done. This directive is
scoped to the three items above, Item 1 being the one that actually matters for
production reliability.

## Closing Condition

Item 1's quantified before/after fix for L2, Item 2's named diff, and Item 3's
explicit confirmation. Given how narrow and specific these three are, this is
very likely the last round.
