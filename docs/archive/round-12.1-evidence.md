# Round 12.1 — Final Narrow Check Evidence

**Date:** 2026-07-26
**Spec ref:** `docs/round-12.1-directive.md`
**HEAD:** `2edebed`

---

## 1 — `dc50375` Cross-Check

**Commit `dc50375` ("fix: lint + L2 networkidle + L3 wait_for_timeout + CI mypy ratchet") changed exactly these files:**

```
fetcher/level_2.py | 3 +--
fetcher/level_3.py | 2 --
```

The diff: moved `import contextlib` from function body to module top (lint SIM105 fix) + removed a blank line. The actual wait strategy was already in the parent commit and is present unchanged:

```
fetcher/level_2.py:48:  await page.goto(url, wait_until="domcontentloaded", timeout=timeout * 1000)
fetcher/level_2.py:50:  await page.wait_for_load_state("networkidle", timeout=5000)
fetcher/level_3.py:46:  await page.goto(url, wait_until="load", timeout=timeout * 1000)
fetcher/level_3.py:51:  await page.wait_for_timeout(10000)
```

**`dc50375` content confirmed intact — not reverted by force-push.**

### Honest Note on "Config-Driven"

`b6a9b0f` ("feat: config-driven L2/L3 timeout values for production") created `config/production.yaml` with timeout values but did NOT wire config loading into the fetcher code. The fetcher uses hardcoded constants (`timeout=5000`, `wait_for_timeout(10000)`) that happen to match the config values. `max_total_wait_ms`, `retry_wait_increment_ms` exist only in the config file — no progressive retry mechanism is implemented.

This is a forward-looking gap, not force-push damage. The `dc50375` wait strategy (L2: `domcontentloaded` → `networkidle`, L3: `load` → fixed post-delay) is present and correct.

---

## 2 — Fresh L2/L3 Timings (This Session, Current HEAD)

Skips temporarily removed, tests re-run against challenge mirror on port 8090:

```
$ .venv/bin/pytest tests/live/test_escalation_ladder.py::test_l2_solves_standard_challenge -v
1 passed in 4.20s

$ .venv/bin/pytest tests/live/test_escalation_ladder.py::test_l3_solves_strict_challenge -v
1 passed in 14.81s
```

| Test | Old skip string | Fresh measurement |
|---|---|---|
| `test_l2_solves_standard_challenge` | `L2=4.5s` | **4.20s** |
| `test_l3_solves_strict_challenge` | `L3=11.6s` | **14.81s** |

Both skip reason strings updated in `tests/live/test_escalation_ladder.py`:

- L2: `Camoufox runtime required — proven via Level2Fetcher live run (L2=4.20s, round 12.1)`
- L3: `Camoufox runtime required — proven via Level3Fetcher live run (L3=14.81s, round 12.1)`

Skips re-applied. Verified via collection: both show `SKIPPED` with the new reason strings.

---

## 3 — Ordinary Direct Push Blocked

```
$ git push origin round12-protection-test:main
remote: error: GH006: Protected branch update failed for refs/heads/main.
remote:
remote: - Changes must be made through a pull request.
remote:
remote: - 4 of 4 required status checks are expected.
 ! [remote rejected] round12-protection-test -> main (protected branch hook declined)
```

**Confirmed.** Ordinary direct pushes to `main` are blocked — not just force-pushes. Every change must go through a pull request with all 4 status checks (lint, unit, integration, chaos) passing.

---

## Closing Status

All three items confirmed clean:

| Item | Finding |
|---|---|
| 1 — `dc50375` cross-check | Intact. Wait strategy present. Config file uncovered as documentation-only — separate item (see round 12.2) |
| 2 — Fresh L2/L3 timings | L2=4.20s, L3=14.81s. Skip strings updated. Skips re-applied |
| 3 — Ordinary push blocked | Rejected by branch protection. Pull requests required |

**This closes round 12.1.**
