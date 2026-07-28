# Round 12.1 — Final Narrow Check Before Declaring This Closed

Three items, all small, all specific to one gap: the 8-item deliverable
cross-check in section 1.3 never actually checked `dc50375` — the exact commit
this report itself names as the collateral damage from the reset. Everything
else checked out; this is the one loose thread.

---

## 1 — `dc50375` Was Never Cross-Checked, and It's the One Commit That Matters Most Here

Section 1.3 grep-confirms 8 deliverables. `dc50375` — "fix: lint + L2
networkidle + L3 wait_for_timeout + CI mypy ratchet," the commit explicitly
named as wiped by the reset — is not one of them. Given this is the specific,
named casualty of the incident this whole round is about, it should have been
the first thing checked, not the one thing left out.

```bash
grep -n "wait_until\|networkidle\|wait_for_timeout\|max_total_wait_ms" fetcher/level_2.py fetcher/level_3.py
```
Paste the output. Confirm the config-driven version from round 11 (bounded
`max_total_wait_ms`, `retry_wait_increment_ms`, config-sourced rather than
hardcoded) is what's actually present — not a reversion to the flat, unbounded
`wait_for_timeout(10000)` from an earlier round.

## 2 — Re-Run L2/L3 Live, Fresh, This Session — Not a Cited Comment

The skip reasons for `test_l2_solves_standard_challenge` and
`test_l3_solves_strict_challenge` cite `L2=4.5s` and `L3=11.6s` as proof the
underlying code works. Those specific numbers don't match this project's own
round 10/11 timings for the *current* config-driven fetcher (which measured
closer to L2≈4s / L3≈15s under the retry-increment approach) — they read like
leftover text from an earlier, different measurement, possibly the
challenge-mirror's own standalone SHA-256 verification from several rounds back,
not a fresh run of the actual `Level2Fetcher`/`Level3Fetcher` code as it exists
right now post-restoration. A comment string is not evidence, especially not
after a round whose entire subject was "things believed to be true turned out to
have been silently reverted."

**Required:** temporarily un-skip both tests, run them for real, in this
session, against current `HEAD`:
```bash
.venv/bin/pytest tests/live/test_escalation_ladder.py::test_l2_solves_standard_challenge \
  tests/live/test_escalation_ladder.py::test_l3_solves_strict_challenge -v -s
```
Paste the actual current timings. Update the skip-reason strings in the test
file to match, if they've drifted. Re-apply the skip markers afterward if still
appropriate for CI resource reasons — but this session's run must produce fresh
numbers, not repeat old ones.

## 3 — Confirm Branch Protection Blocks *Ordinary* Direct Pushes Too, Not Just Force-Pushes

The verified settings (`allow_force_pushes: false`, `allow_deletions: false`,
`enforce_admins: true`) directly close the exact vector that caused this
incident. One adjacent thing worth confirming while this is fresh: does the
current configuration also require changes to land via a reviewed PR (even with
0 required approvals) rather than an ordinary, non-force `git push` straight to
`main`? If a plain `git push origin main` after a local `git reset --hard` is
still possible, the same *category* of accident (rewriting local history, then
pushing it, just without needing `--force` because nothing forced was required)
remains open, only slightly narrower than before.

```bash
# from a scratch branch state, attempt an ordinary direct push to main
git checkout main && git commit --allow-empty -m "protection test" && git push origin main
```
Paste the result — it should be rejected with a message requiring a pull
request. If it succeeds, tighten branch protection further (`restrictions` or
requiring PRs explicitly) and re-verify.

---

## Closing Condition

If 1 and 2 confirm the current fetcher code and its real, current timings
line up, and 3 confirms ordinary direct pushes are also blocked (or gets
tightened until they are), **this closes the project.** Nothing else is being
held open pending this — it's a narrow, final check on the one thing this
report's own investigation flagged as damaged and didn't quite finish verifying.
