#!/usr/bin/env python3
"""Coverage gate: 100% of lines, plus a shrinking budget for branches.

Round 62. Why this exists rather than a single `--cov-fail-under`:

A coverage audit found the project's headline "real 100% coverage" was
100% of LINES only — `branch = true` had never been set. Inside those same
100%-covered files, 33 decision branches had never been taken one way. The
worst of them was `fetcher/level_1.py`'s `for _ in range(MAX_REDIRECTS)`
loop, which no test ever drove to exhaustion, so redirect-limit behaviour
was entirely unverified while the file reported perfect coverage.

Turning branches on drops the combined figure to ~99.33%, so a plain
`--cov-fail-under` has to be lowered to pass — and a lowered percentage
gate is strictly weaker than what was there before, because it would also
start tolerating missed LINES. A 0.67% allowance is roughly 33 statements
of slack that did not exist yesterday.

This script keeps both guarantees separately and exactly:

  * lines: zero misses, no tolerance at all — the pre-existing guarantee,
    unchanged;
  * branches: at most BRANCH_BUDGET misses, an absolute count rather than
    a percentage, so it cannot be diluted by simply adding more code.

The budget is a ratchet. It may only ever be lowered. When the real count
drops below it this script FAILS and tells you to lower the constant —
otherwise a budget set once silently becomes permission to regress back up
to it later.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# Packages the gate covers. Mirrors [tool.coverage.report] include in
# pyproject.toml. browser/ is deliberately absent: it needs a real Firefox
# process (~80MB RSS) that CI runners do not have, so it is measured where
# possible and never gated — see that file's comment.
GATED_PREFIXES = (
    "core/",
    "proxy/",
    "orchestrator/",
    "fetcher/",
    "services/",
    "storage/",
    "api/",
)

# Ratchet. Lower this whenever the real count drops; never raise it.
# 2026-09-20 (round 62): 33, the audit baseline.
BRANCH_BUDGET = 33


def main() -> int:
    report_path = Path(sys.argv[1] if len(sys.argv) > 1 else "coverage.json")
    if not report_path.exists():
        print(f"ERROR: {report_path} not found. Run pytest with --cov-report=json:{report_path}")
        return 2

    data = json.loads(report_path.read_text())
    line_offenders: list[str] = []
    branch_offenders: list[tuple[str, int]] = []
    missing_lines = 0
    missing_branches = 0

    for path, entry in sorted(data["files"].items()):
        rel = path.split("src/scraper_engine/")[-1]
        if not rel.startswith(GATED_PREFIXES):
            continue
        summary = entry["summary"]
        ml = summary["missing_lines"]
        mb = summary.get("missing_branches", 0)
        missing_lines += ml
        missing_branches += mb
        if ml:
            line_offenders.append(f"  {rel}: {ml} uncovered line(s) -> {entry['missing_lines']}")
        if mb:
            branch_offenders.append((rel, mb))

    failed = False

    if missing_lines:
        print(f"FAIL: {missing_lines} uncovered line(s) in gated packages (budget: 0)")
        print("\n".join(line_offenders))
        failed = True
    else:
        print("OK — 0 uncovered lines in gated packages.")

    if missing_branches > BRANCH_BUDGET:
        print(f"FAIL: {missing_branches} uncovered branches exceeds budget {BRANCH_BUDGET}")
        for rel, mb in sorted(branch_offenders, key=lambda x: -x[1]):
            print(f"  {rel}: {mb}")
        print("\nCover the new branches. Do NOT raise BRANCH_BUDGET — it only goes down.")
        failed = True
    elif missing_branches < BRANCH_BUDGET:
        print(
            f"FAIL (ratchet): {missing_branches} uncovered branches is BELOW the budget "
            f"{BRANCH_BUDGET}. Lower BRANCH_BUDGET to {missing_branches} in "
            f"tools/check_coverage_ratchet.py to lock the improvement in."
        )
        failed = True
    else:
        print(f"OK — {missing_branches} uncovered branches, exactly at budget {BRANCH_BUDGET}.")

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
