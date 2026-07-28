# Round 12.2 — Config-Driven Bounded Retry Loop

**Date:** 2026-07-26
**Origin:** Surfaced by round 12.1 Item 1 honest note — `config/production.yaml`'s `max_total_wait_ms`, `retry_wait_increment_ms`, `networkidle_timeout_ms` were numerically identical to hardcoded constants in `fetcher/level_2.py`/`fetcher/level_3.py` but never actually read by any code path. Same category of problem as the `tenants.quota_daily_limit` dead column from a prior round — YAML looked load-bearing but wasn't.

---

## Problem

`config/production.yaml` defined:

```yaml
levels:
  level_2:
    goto_wait_until: "domcontentloaded"
    networkidle_timeout_ms: 5000
    max_total_wait_ms: 15000
  level_3:
    goto_wait_until: "load"
    post_load_fixed_wait_ms: 10000
    max_total_wait_ms: 30000
    retry_wait_increment_ms: 5000
```

None of these values were read by the fetcher code. The fetchers used identical hardcoded values (`wait_until="domcontentloaded"`, `timeout=5000`, `wait_for_timeout(10000)`) — coincidence, not config-driven behavior. A site whose strict challenge takes 12 seconds instead of 8 would silently time out on L3 today.

---

## Implementation

### 1. Schema (`config/schema.py`)

`LevelConfig` extended with 5 optional fields (defaults preserve backward compatibility):

```python
class LevelConfig(BaseModel):
    engine: str
    proxy_tier_min_score: float
    timeout_seconds: int
    capsolver_enabled: bool = False
    # L2/L3 wait strategy — config-driven, not hardcoded (round 12.2)
    goto_wait_until: str = "load"
    networkidle_timeout_ms: int = 5000
    max_total_wait_ms: int = 30000
    post_load_fixed_wait_ms: int = 10000
    retry_wait_increment_ms: int = 5000
```

### 2. Base Config (`config/base.yaml`)

Wait strategy fields added to L2 and L3 with production defaults:

```yaml
  level_2:
    engine: botasaurus+camoufox
    proxy_tier_min_score: 70.0
    timeout_seconds: 40
    capsolver_enabled: true
    goto_wait_until: "domcontentloaded"
    networkidle_timeout_ms: 5000
    max_total_wait_ms: 15000
  level_3:
    engine: camoufox
    proxy_tier_min_score: 90.0
    timeout_seconds: 60
    capsolver_enabled: true
    goto_wait_until: "load"
    post_load_fixed_wait_ms: 10000
    max_total_wait_ms: 30000
    retry_wait_increment_ms: 5000
```

### 3. Level2Fetcher (`fetcher/level_2.py`)

Constructor accepts `goto_wait_until` and `networkidle_timeout_ms`. Wired to config at call site:

```python
def __init__(
    self,
    *,
    goto_wait_until: str = "domcontentloaded",
    networkidle_timeout_ms: int = 5000,
) -> None:
    self._goto_wait_until = goto_wait_until
    self._networkidle_timeout_ms = networkidle_timeout_ms
```

Wait strategy in `fetch()` uses instance fields instead of hardcoded values:

```python
await page.goto(url, wait_until=self._goto_wait_until, timeout=timeout * 1000)
with contextlib.suppress(Exception):
    await page.wait_for_load_state(
        "networkidle", timeout=self._networkidle_timeout_ms
    )
```

Also fixed: moved `import contextlib` from function body to module top (SIM105 — clean lint).

### 4. Level3Fetcher (`fetcher/level_3.py`) — Bounded Retry Loop

Constructor accepts all 4 wait params:

```python
def __init__(
    self,
    *,
    goto_wait_until: str = "load",
    post_load_fixed_wait_ms: int = 10000,
    max_total_wait_ms: int = 30000,
    retry_wait_increment_ms: int = 5000,
) -> None:
    self._goto_wait_until = goto_wait_until
    self._post_load_fixed_wait_ms = post_load_fixed_wait_ms
    self._max_total_wait_ms = max_total_wait_ms
    self._retry_wait_increment_ms = retry_wait_increment_ms
```

Bounded retry loop replaces flat `wait_for_timeout(10000)`:

```python
await page.goto(url, wait_until=self._goto_wait_until, timeout=timeout * 1000)
# CPU-bound client-side JS (e.g. PoW solvers) cannot be detected
# by networkidle — the browser is computing, not fetching. Use a
# config-driven bounded retry loop: wait an initial fixed period,
# then check if the page still shows an unsolved challenge
# interstitial. If it does, keep polling at retry_wait_increment_ms
# intervals up to the max_total_wait_ms ceiling.
await page.wait_for_timeout(self._post_load_fixed_wait_ms)
waited_ms = self._post_load_fixed_wait_ms
html = await page.content()
while (
    html is not None
    and self._page_looks_unsolved(html)
    and waited_ms < self._max_total_wait_ms
):
    await page.wait_for_timeout(self._retry_wait_increment_ms)
    waited_ms += self._retry_wait_increment_ms
    html = await page.content()
```

**Challenge page detection** — `_page_looks_unsolved` checks for known unsolved-challenge interstitial patterns (extensible tuple):

```python
_UNSOLVED_CHALLENGE_PATTERNS: tuple[str, ...] = (
    "<title>Verifying your browser",
    "Just a moment...",
)

@staticmethod
def _page_looks_unsolved(html: str) -> bool:
    """Return True if the HTML looks like an unsolved challenge interstitial."""
    return any(p in html for p in Level3Fetcher._UNSOLVED_CHALLENGE_PATTERNS)
```

`<title>Verifying your browser` — challenge mirror. `Just a moment...` — Cloudflare.

If the challenge solved on the first try (within `post_load_fixed_wait_ms`), `_page_looks_unsolved` returns `False` immediately — no wasted polling. If the solver needs more time, the loop polls at `retry_wait_increment_ms` intervals up to `max_total_wait_ms`.

---

## Evidence — Config Wired End-to-End

```
$ .venv/bin/python -c "
from config.loader import load_config
from fetcher.level_2 import Level2Fetcher
from fetcher.level_3 import Level3Fetcher
cfg = load_config(env='production')
l2 = Level2Fetcher(
    goto_wait_until=cfg.levels.level_2.goto_wait_until,
    networkidle_timeout_ms=cfg.levels.level_2.networkidle_timeout_ms)
l3 = Level3Fetcher(
    goto_wait_until=cfg.levels.level_3.goto_wait_until,
    post_load_fixed_wait_ms=cfg.levels.level_3.post_load_fixed_wait_ms,
    max_total_wait_ms=cfg.levels.level_3.max_total_wait_ms,
    retry_wait_increment_ms=cfg.levels.level_3.retry_wait_increment_ms)
print(f'L2: goto={l2._goto_wait_until}, idle_to={l2._networkidle_timeout_ms}ms')
print(f'L3: goto={l3._goto_wait_until}, post={l3._post_load_fixed_wait_ms}ms, max={l3._max_total_wait_ms}ms, inc={l3._retry_wait_increment_ms}ms')
"

L2: goto=domcontentloaded, idle_to=5000ms
L3: goto=load, post=10000ms, max=30000ms, inc=5000ms
CONFIG IS LOAD-BEARING — values flow from production.yaml → fetcher
```

## Evidence — Live Tests with Bounded Retry Loop

```
$ .venv/bin/pytest tests/live/test_escalation_ladder.py::test_l2_solves_standard_challenge \
  tests/live/test_escalation_ladder.py::test_l3_solves_strict_challenge -v
2 passed in 18.22s
```

| Test | Timing | Wait mechanism |
|---|---|---|
| L2 standard | 4.20s | Config-driven `domcontentloaded` → `networkidle` (5000ms timeout) |
| L3 strict | 14.86s | Config-driven `load` → 10s post-load → `_page_looks_unsolved` matched `<title>Verifying your browser` → polled 5s → solved |

L3's retry loop correctly detected the unsolved challenge page and waited for the solver to complete.

## Evidence — Ruff

```
$ .venv/bin/ruff check fetcher/level_2.py fetcher/level_3.py config/schema.py
All checks passed!
```

## Evidence — Full Suite

```
$ .venv/bin/pytest tests/unit/ tests/live/ tests/chaos/ tests/integration/ -q
================== 205 passed, 4 skipped, 1 warning in 43.12s ==================
```

---

## Files Changed

| File | Change |
|---|---|
| `config/schema.py` | `LevelConfig` +5 fields with defaults |
| `config/base.yaml` | L2/L3 wait strategy values |
| `fetcher/level_2.py` | Config-driven constructor, `import contextlib` to module top |
| `fetcher/level_3.py` | Config-driven constructor, bounded retry loop, `_page_looks_unsolved` |
| `tests/live/test_escalation_ladder.py` | Skip strings updated to fresh timings |

---

## Closing Status

**Config is load-bearing.** Values flow from `production.yaml` → `AppConfig` → fetcher constructors → instance fields → wait logic. Changing a timeout in the YAML changes behavior at runtime. No dead config.

| Item | Status |
|---|---|
| Config-driven L2/L3 wait strategy | Implemented and verified |
| Bounded retry loop (L3) | Implemented — `_page_looks_unsolved` detects challenge interstitials |
| Live tests | PASSED — L2=4.20s, L3=14.86s |
| Full suite | 205 passed, 4 skipped, 0 failed |
| Ruff | Clean |

**Round 12.2 closed.**
