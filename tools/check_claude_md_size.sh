#!/bin/bash
# tools/check_claude_md_size.sh
#
# Hard gate on CLAUDE.md's word count. CLAUDE.md is always loaded into every
# session, so it must stay small, stable, navigation-only — never a
# round-by-round diary. That exact regression (the file accumulating dated
# per-round narrative instead of pointing to .claude/knowledge/technical-debt.md)
# has been manually caught and re-trimmed three times (round 28, round 57,
# and a 2026-08-18 knowledge audit — see .claude/knowledge/decisions.md ->
# "Knowledge-Audit: CLAUDE.md Diary Regression, 3rd Occurrence"). This script
# turns that manual catch into a mechanical one so a fourth occurrence fails
# CI instead of waiting for the next audit.
#
# Threshold rationale: CLAUDE.md sat at 1557 words right after the 2026-08-18
# trim (13.8KB). 1800 gives real per-PR headroom for legitimate navigation
# additions without silently tolerating diary regrowth back toward the
# pre-trim 31.7KB/~4900-word size.
set -euo pipefail

FILE="CLAUDE.md"
MAX_WORDS=1800

if [ ! -f "$FILE" ]; then
  echo "No $FILE at repo root — nothing to check."
  exit 0
fi

WORDS=$(wc -w < "$FILE" | tr -d ' ')

echo "$FILE: $WORDS words (limit: $MAX_WORDS)"

if [ "$WORDS" -gt "$MAX_WORDS" ]; then
  echo ""
  echo "FAILING: $FILE has grown past $MAX_WORDS words."
  echo "This is very likely the same diary-regrowth pattern fixed at round 28,"
  echo "round 57, and 2026-08-18 — dated per-round narrative creeping back into"
  echo "an always-loaded file instead of living in .claude/knowledge/technical-debt.md."
  echo "Before adding more here: does this belong in technical-debt.md or"
  echo "decisions.md instead, with just a one-line pointer added here?"
  exit 1
fi

echo "OK — $FILE within size gate."
