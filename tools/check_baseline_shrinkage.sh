#!/bin/bash
# tools/check_baseline_shrinkage.sh (round 13 E1)
#
# Advisory CI check for the round-11 standing rule: "any PR touching a file
# listed in tools/mypy-baseline.txt should reduce that file's entries." This
# turns the rule from an unenforced convention into a visible CI warning.
#
# Deliberately ADVISORY (warns, exit 0) rather than a hard gate: forcing every
# incidental touch of a baseline file to also fix unrelated type errors could
# itself become a source of scope-creep and rushed fixes — exactly the failure
# mode this project has guarded against. A visible warning is enough to keep the
# rule from being forgotten, without weaponising it.
set -euo pipefail

BASELINE="tools/mypy-baseline.txt"
if [ ! -f "$BASELINE" ]; then
  echo "No $BASELINE — nothing to check."
  exit 0
fi

# Files changed vs main. Falls back to staged changes if the merge-base is absent
# (e.g. a shallow local run outside CI).
if git rev-parse --verify origin/main >/dev/null 2>&1; then
  CHANGED_FILES=$(git diff --name-only origin/main...HEAD -- '*.py' || true)
else
  CHANGED_FILES=$(git diff --name-only --cached -- '*.py' || true)
fi

BASELINE_FILES=$(cut -d: -f1 "$BASELINE" | sort -u)

flagged=0
for f in $CHANGED_FILES; do
  if echo "$BASELINE_FILES" | grep -qx "$f"; then
    REMAINING=$(grep -c "^$f:" "$BASELINE" || true)
    echo "WARNING: $f is in the mypy baseline ($REMAINING entries) and was modified this PR."
    echo "         Per project policy, PRs touching baseline-listed files should reduce that file's entries."
    flagged=1
  fi
done

if [ "$flagged" -eq 0 ]; then
  echo "No modified files are in the mypy baseline — nothing to shrink."
fi

# Advisory only — never fails the build.
exit 0
