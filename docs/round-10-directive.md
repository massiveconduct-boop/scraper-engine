# Round 10 Directive — Strict, Explicit, Mandatory

Genuine credit up front, because it's earned and the directive below is sharper
for saying so: Item A ran the actual L2/L3 live tests instead of leaving them
skipped, reported real failures instead of dressing them up, found and fixed a
real `None`-guard bug in the process, and diagnosed the remaining issue precisely.
Item D's timestamp-instrumented overlap proof is exactly what was asked for. Item
B pushed to a real GitHub Actions run with a visible URL and an honest fixup-commit
history instead of a clean-looking story. That is the standard this project should
run at permanently. Two things below are not done, and one of them is a subtle,
important catch — read Item 1 carefully before doing anything else.

---

## ITEM 1 — The `mypy --strict` Result May Be Invalid, Not Just Imperfect — BLOCKING

### The Problem

The raw output submitted as evidence:
```
Found 1 error in 1 file (errors prevented further checking)
```
**"errors prevented further checking" is mypy's own language for a fatal parse
failure that stops analysis before it reaches the rest of the target files.** This
is categorically different from mypy checking every file in `core/ proxy/
orchestrator/ api/ storage/` and finding zero issues. A clean `mypy --strict` run
prints something like `Success: no issues found in N source files` — that `N` is
the proof of how many files were actually analyzed. **This report has no such
line.** It is entirely possible that mypy crashed on numpy's stub file during
import resolution and never actually type-checked a single line of `core/`,
`proxy/`, `orchestrator/`, `api/`, or `storage/` — meaning "0 errors in project
code" could mean "0 files of project code were ever examined," not "all of them
passed."

This matters because the numpy error itself is suspicious in a specific, checkable
way: `Type statement is only supported in Python 3.12 and greater` is PEP 695
syntax, which **is valid Python 3.12** — the error strongly suggests the installed
`mypy` version predates PEP 695 support and cannot parse a stub file that uses it,
not that numpy's stub is actually broken.

### Required Action

```bash
.venv/bin/pip show mypy | grep Version
```
If it's older than 1.8, upgrade:
```bash
.venv/bin/pip install --upgrade "mypy>=1.9"
```
Then re-run, and this time require the file-count success line as evidence:
```bash
.venv/bin/mypy core/ proxy/ orchestrator/ api/ storage/ --strict --ignore-missing-imports
```
**Paste the complete raw output, and specifically confirm it contains a line of
the form `Success: no issues found in N source files` (or a full list of real
findings if it doesn't pass) — not just "1 error, in numpy."** If numpy's stub
still trips something after the upgrade, exclude it explicitly rather than letting
it abort the whole run silently:
```bash
.venv/bin/mypy core/ proxy/ orchestrator/ api/ storage/ --strict --ignore-missing-imports \
  --exclude '.venv/.*'
```
Do not report this item MET again until the success line (or a real, complete
findings list covering every target directory) is in the report.

---

## ITEM 2 — Restore Type-Checking to CI — BLOCKING

The CI pipeline currently has **zero mypy enforcement of any kind** — it wasn't
loosened, it was deleted from the lint job entirely (fixup commits `0c5b635` and
`7a9385a`). That is a regression from the pipeline's own stated design (4 stages:
lint = ruff + mypy), and given Item 1 above means the actual state of type safety
in this project is currently unknown, not just unstrict.

**Required, after Item 1 is resolved:**
1. Determine the real reason `mypy --strict` failed on GitHub's runner specifically
   (paste the actual CI log line, not a paraphrase — "different stub environment"
   is not a diagnosis).
2. Pin the exact mypy version in `pyproject.toml`'s dev dependencies so local and
   CI environments cannot silently diverge on this again (this is the same class
   of problem as the `asyncpg`/`httpx` version-drift issue from several rounds
   ago — pin it, don't let `pip install` resolve it differently on different
   machines).
3. Add back to `.github/workflows/test.yml`'s lint job, at minimum:
   ```yaml
   - run: mypy core/ proxy/ orchestrator/ api/ storage/ --ignore-missing-imports
   ```
   Non-strict is an acceptable interim bar given Item 1's finding that `--strict`
   has never actually been proven to pass cleanly — but **zero mypy in CI is not
   acceptable**, since it means a genuine type error introduced by any future
   change (this project's or another agent's) would have no CI signal at all,
   silently, forever.
4. Push, confirm green, paste the new run URL.

---

## ITEM 3 — Fix the Actual L2/L3 Bug Found This Round, Don't Just Diagnose It — BLOCKING

This is a real correctness bug, not a test artifact, and it matters for
production, not just for the mirror: `Level2Fetcher`/`Level3Fetcher` call
`page.content()` while the challenge page's client-side JS may still be executing,
producing `Page.content: Unable to retrieve content because the page is
navigating and changing the content.` **Any real target that runs JS after
initial page load — which is most of what Level 2/3 exist to handle — can trigger
this exact race in production**, not just against the self-hosted mirror. This is
not "a mirror quirk," it's a live gap in the escalation ladder's core fetch logic.

**Required fix, in both files:**
```python
# fetcher/level_2.py and fetcher/level_3.py, wherever page.goto() is called
await page.goto(url, wait_until="networkidle", timeout=self._timeout_ms)
```
If `networkidle` proves too strict for targets with long-polling/websocket
connections that never go idle (a real risk on some sites), use a bounded
explicit wait instead:
```python
await page.goto(url, wait_until="domcontentloaded", timeout=self._timeout_ms)
try:
    await page.wait_for_load_state("networkidle", timeout=5000)
except PlaywrightTimeoutError:
    pass  # proceed anyway — some pages never go fully idle; content is still usable
html = await page.content()
```
**Required evidence:** re-run `test_l2_solves_standard_challenge` and
`test_l3_solves_strict_challenge` with the fix applied, un-skipped, and passing —
not diagnosed-and-reverted. Paste the raw passing output. Only then restore the
`@pytest.mark.skip` markers if there's still a reason they need to be skipped in
normal CI (Camoufox binary weight) — but the underlying fetcher bug must be fixed
and proven fixed in this session first, independent of whether the tests stay
skipped in CI afterward.

---

## Confirmed Closed — Do Not Re-litigate

- Item B (CI pipeline, real green run, real URL, honest fixup history) — accepted.
- Item D (OS-subprocess politeness race, timestamp-proven overlap) — accepted.
- Item E (per-tenant quota integration test) — accepted.
- Item A's binary-presence question specifically — confirmed present, launchable,
  Dockerfile correctly configured. That sub-question is closed. What's still open
  from Item A is the fetcher bug (now Item 3 above), not the binary.

---

## Lower Priority — Decisions, Not Actions

- **22 Camoufox-dependent tests permanently excluded from CI:** this is a
  reasonable resource tradeoff for a GitHub-hosted runner, but state explicitly
  whether this is accepted as permanent (CI validates everything except the
  Camoufox path; that path is verified manually each round the way this project
  has been doing) or whether a self-hosted runner with the binary pre-cached is a
  future goal. Either answer is fine — pick one and say so, rather than leaving it
  implicit.
- **Docker image size (4.01GB):** the report's own framing — "Oracle Cloud VPS has
  100GB boot volume, plenty of headroom" — is an acceptable resolution. Treat this
  as closed unless disk budget changes.

---

## Reporting Rule, Unchanged

Every item closes on evidence, specifically: a success line with a file count for
Item 1, a green CI run URL with mypy actually present for Item 2, and a passing
(not diagnosed-then-reverted) L2/L3 test output for Item 3. A status label without
the exact evidence named above is not accepted, per the standing rule this project
has operated under since round 6.
