# Round 10.02 Directive — One Question, Answered With Evidence, Not Description

The ratchet mechanism itself (`comm -13` against a committed baseline, `exit 1` on
anything new) is the right design — real progress from `|| true`. But there's a
mismatch in what's been shown that could make it pass trivially regardless of
whether real regressions occur, and it needs to be settled with file contents, not
another summary paragraph.

---

## The Mismatch

`tools/mypy-baseline.txt` is described as **"23 known non-strict findings across 6
files."** But the only 23-error, 6-file output shown anywhere in this project's
evidence — in this exact report, under the heading "Why `--strict` Failed" — was
produced by a command that **includes `--strict`**:
```
Run mypy core/ proxy/ orchestrator/ api/ storage/ --strict --ignore-missing-imports
...
Found N errors in 6 files (checked 35 source files)
```
The CI ratchet step that actually gates the build runs a **different command, without
`--strict`**:
```yaml
mypy core/ proxy/ orchestrator/ api/ storage/ --ignore-missing-imports >/tmp/mypy.out 2>&1 || true
```

Several of the specific findings in that 23-error list — `[untyped-decorator]`,
`[no-any-return]`, the `BaseModel`/`BaseHTTPMiddleware` "has type Any" `[misc]`
errors — are error categories that `mypy` commonly does not surface at all without
`--strict` (or the specific flags `--strict` bundles, like
`--disallow-untyped-decorators` and `--warn-return-any`). **If the baseline file
was generated from the `--strict` run, but the live CI check runs without
`--strict`, the two are checking different things.** In that case the live check's
`/tmp/mypy-current.txt` is likely near-empty — not because the codebase improved,
but because the weaker command structurally can't produce most of the baseline's
error categories. `comm -13` would then almost always report "nothing new,"
**not because regressions are being caught, but because the check that would catch
them was quietly swapped for one that can't.** That's the same shape of problem as
`|| true` — a green checkmark that doesn't mean what it appears to mean — just one
layer more disguised, behind a genuinely well-built diffing mechanism.

## Required — Direct Evidence, Not a Restated Explanation

1. **Paste the exact command used to generate `tools/mypy-baseline.txt`.** Not a
   description — the literal shell command from history or a script, e.g.
   `mypy ... --strict ... > tools/mypy-baseline.txt` or without `--strict`. Whatever
   flags were used there, the live CI check must use the identical flags. If they
   don't match right now, that's the bug — fix by making the ratchet step's command
   and the baseline-generation command identical (both `--strict` or both without).

2. **Paste `cat tools/mypy-baseline.txt`** in full. Confirm it actually contains 23
   lines matching the format `file:line: error: ... [code]`.

3. **Paste the full, unedited contents of `/tmp/mypy-current.txt`** from an actual
   run of the exact CI ratchet command, executed locally right now:
   ```bash
   mypy core/ proxy/ orchestrator/ api/ storage/ --ignore-missing-imports >/tmp/mypy.out 2>&1 || true
   grep "^[^ ]*:[0-9]*: error:" /tmp/mypy.out | sort >/tmp/mypy-current.txt
   cat /tmp/mypy-current.txt
   ```
   If this file is empty, or has only 1-2 lines, while the baseline has 23 — that
   confirms the mismatch and the flags must be aligned per step 1.
   If it genuinely has ~23 matching lines, the mismatch concern is resolved — but
   the file contents need to actually be shown, not asserted, to prove it.

4. **Then, prove the ratchet actually fails on a real new error** — the one test
   that settles this definitively: introduce one deliberate, trivial new typing
   violation in a scratch file or temporarily in a real file (e.g., an untyped
   function parameter in a file already covered by the glob), run the exact ratchet
   script locally, and paste output showing `exit 1` and the `=== NEW mypy errors
   (failing build) ===` block naming that specific injected error. Then revert it.
   This is the single piece of evidence that proves the gate can actually catch
   something, as opposed to always reporting clean because of the flag mismatch in
   item 1.

## Not Reopening Anything Else

Item 1 (mypy version, root cause of the original 3.11/PEP-695 crash) and Item 3
(L2/L3 fetcher fix, including the level_3-specific `networkidle`-doesn't-apply
reasoning, which is a sound technical distinction this time, not a shortcut) are
accepted and closed. This directive is scoped to the ratchet-validity question
alone.
