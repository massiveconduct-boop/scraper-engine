# Round 12 Directive — The Force-Push Is The Story Here, Not The Test Count

## Read This First

Buried in Item 1's gap-analysis table and the "Force-Push Restoration Commits"
section is the actual headline finding of this entire report: **at some point
after round 10.03, `main` was force-pushed back to a pre-round-9 commit
(`9432224`), destroying multiple rounds of verified, evidence-backed work** —
not just test files, but production code: `browser/pool.py`,
`camoufox_wrapper.py`, `api/routes.py`, `api/main.py`, `observability/metrics.py`
all had to be restored from scratch (commit `e0a532c`), and the specific
`core/quota.py` per-tenant Redis-key fix from round 8 — a bug that took real
debugging effort to find the first time — **had to be rediscovered and refixed
as if it were new**, because the fix had been silently erased.

This is not "12 missing tests, explained." This is: **the mechanism that
protects this project's completed work has no floor under it, and it already
failed once.** A single force-push command destroyed rounds of verified
engineering, and the only reason it was caught is that this round happened to
re-run the full suite and notice the count was wrong. If that hadn't happened,
this project could have shipped with the round-8 cross-tenant quota bug back in
production, silently, with every prior report's "MET" status still sitting in
the historical record as if it were still true.

**This is Item 1 below. It is the priority. Nothing else in this directive
matters if this recurs.**

---

## ITEM 1 — Diagnose and Prevent the Force-Push — BLOCKING, HIGHEST PRIORITY

### Required Answers, Not Summaries

1. **What command or process caused the force-push to `9432224`?** Paste
   `git reflog show main` (or the fullest history available) covering the period
   between round 10.03's last commit and this round's `e0a532c`. Identify the
   exact `git push --force` (or equivalent: a bad rebase, a bad `git reset --hard`
   + push, a CI/deploy step that reset a branch) that caused this.
2. **Was this deliberate or accidental?** If deliberate (e.g., an attempt to undo
   an unrelated bad commit that swept up good commits along with it), explain
   what the original intent was and why force-push was the tool reached for
   instead of a targeted revert.
3. **Is there any other work, beyond the 12 tests and 5 production files already
   identified, that might still be silently missing?** Do not assume the
   restoration is complete just because the test count now matches. Cross-check
   against this project's actual historical deliverable list — at minimum,
   confirm each of the following is present in current `HEAD` with a `grep`/`git
   log -p` citation, not an assertion:
   - `core/tenant.py::TenantId` regex validator (F-10/F-31 closure)
   - `core/ssrf_guard.py::SSRFGuard` with redirect-chain re-validation
   - `browser/pool.py`'s classify-loop double-issue fix (the exact structure
     verified line-by-line in round 8)
   - `api/routes.py`'s full auth + SSRF + quota + DB-persist wiring (the
     "headline finding" fix from round 8)
   - `api/auth.py`'s `revoked_at IS NULL` check
   - `tools/mypy-baseline.txt` and the ratchet CI step
   - `monitoring/alertmanager/alertmanager.yml`'s `send_resolved: true` +
     global `slack_api_url`
   - `challenge-mirror/app/server.py`'s synchronous SHA-256 (not the original
     async `crypto.subtle.digest` version)

### Required Fix — Prevent Recurrence, Not Just Repair Damage

```bash
# Enable branch protection on main — no force-push, no direct push without review
gh api -X PUT repos/{owner}/{repo}/branches/main/protection \
  -f required_status_checks.strict=true \
  -f required_status_checks.contexts[]=lint \
  -f required_status_checks.contexts[]=unit \
  -f required_status_checks.contexts[]=integration \
  -f required_status_checks.contexts[]=chaos \
  -f enforce_admins=true \
  -f required_pull_request_reviews.required_approving_review_count=0 \
  -f restrictions=null \
  -f allow_force_pushes=false \
  -f allow_deletions=false
```
(Adjust `required_approving_review_count` to fit a solo-maintainer workflow — 0
is acceptable if there's no second reviewer, but `allow_force_pushes=false` and
`allow_deletions=false` are non-negotiable regardless of team size.)

**Required evidence:** `gh api repos/{owner}/{repo}/branches/main/protection`
run *after* applying the above, pasted in full, showing
`"allow_force_pushes": {"enabled": false}`. Then tag the current, fully-restored,
fully-verified commit so there is an unambiguous, protected reference point to
recover to if this ever happens again:
```bash
git tag -a v1.0.0-rc1 -m "Round 12: full restoration + branch protection enabled"
git push origin v1.0.0-rc1
```
Paste confirmation the tag exists on the remote.

---

## ITEM 2 — One Canonical Post-Fix Test Run, Not Two Contradictory Ones

This report contains two different collection counts for what's presented as the
same state: Item 1 says **197 collected**, Section 4 says **209 collected**. These
are not actually the same state — Item 1's run predates the fix commits, Section
4's postdates them — but the report never says so explicitly, so as written it
reads as an internal contradiction, and a skimming reader would reasonably be
unsure which number is real. The same problem exists for
`test_pool_full_lifecycle_no_leak` and `test_session_survives_pool_recycle`,
listed as **FAILED** in Item 1's "Retested Tests" table and **PASSED** in Section
4's full run.

**Required:** one single, fresh, complete run, executed *after* Item 1's branch
protection and restoration work is fully committed, with **zero elisions** —
remove every `... (full verbose output continues)` truncation and paste every
single test name and result:
```bash
.venv/bin/pytest tests/unit/ tests/live/ tests/chaos/ tests/integration/ --collect-only -q
.venv/bin/pytest tests/unit/ tests/live/ tests/chaos/ tests/integration/ -v --tb=long
```
This single pair of outputs becomes the one canonical source of truth for this
round. Explicitly state: "collected count matches round 9's baseline of 209 with
zero unexplained gap" — or, if it doesn't match, explain the delta the same way
Item 1 was supposed to, but this time against a state that isn't itself
mid-repair.

**Name the two remaining unlabeled skips.** Section 4's skip table has "2
remaining skips — Camoufox-dependent unit tests with explicit markers" with no
names given. Every skip needs a name and its exact `@pytest.mark.skip(reason=...)`
string, full stop — this project's standing rule against elided evidence applies
to skip reasons as much as to file contents.

---

## ITEM 3 — Re-Verify the Refixed Bugs Weren't Already Fixed Differently Elsewhere

Two of the "bugs found and fixed" in this report are not new discoveries — they
are round-8 and round-9 fixes that were destroyed by the force-push and had to be
redone:
- `core/quota.py:39`'s per-tenant key — identical to round 8's "Tenant Isolation
  Bug — Found and Fixed During Evidence Capture."
- `browser/session_state.py`'s Postgres backend — identical to round 7's
  `SessionStateManager` spec.

**Required:** confirm the *current* fix is byte-identical in behavior to what was
already proven correct in those earlier rounds (re-run the exact two-tenant
quota test from round 8's evidence, and the exact cookie-round-trip test from
round 7's evidence, both against current `HEAD`) — not just that *a* fix exists,
but that it's the *same, already-validated* fix and not a subtly different
reimplementation that happens to pass its own narrower test.

---

## What Closes This Round

1. Item 1's reflog/root-cause answers, branch protection evidence, and the
   pushed `v1.0.0-rc1` tag.
2. Item 2's single, complete, unambiguous test run with the collection-count
   reconciliation stated explicitly.
3. Item 3's re-verification against the original round-7/round-8 test scenarios.

**A report that fixes the immediate test failures without addressing why
completed, verified work was destroyable by a single command in the first place
does not close this round — it treats the symptom and leaves the actual cause
of every future potential regression exactly as exposed as it was going into
this round.**
