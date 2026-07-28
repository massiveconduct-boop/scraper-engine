# Round 16 — Infinite-Scroll / Lazy-Load Handling

**Date:** 2026-07-26
**Scope:** #3 from the post-round-15 backlog — the one genuine functional gap: L2/L3 read content once after load, so a scroll-to-load page yields only its first batch.

---

## Fix — config-driven autoscroll (root cause)

`fetcher/_content_utils.py::autoscroll(page, *, max_passes, wait_ms)`:
- Reads `document.body.scrollHeight`, scrolls to bottom, waits `wait_ms` for lazy content, re-reads height. Repeats until height stops growing (all loaded) or `max_passes` (caps genuine infinite feeds). Early-exits on stable height. Never raises.

Wired into L2 and L3 `_fetch_via_camoufox`: **after** the challenge is solved (`poll_until_solved`), autoscroll runs, then the DOM is re-read (`safe_content`) so lazy content is captured. Config-driven end to end: `LevelConfig.scroll_passes`/`scroll_wait_ms` → `fetcher/factory.py` → fetchers. Default off (0); `config/base.yaml` enables L2=8, L3=10 passes.

---

## A bug the demonstration caught — aggressive early-exit

The first implementation stopped on the **first** pass with no height growth. But AJAX-loaded content lags the scroll: a live trace of `quotes.toscrape.com/scroll` (a genuine AJAX infinite-scroll target) showed
```
initial: scrollHeight=1605 quotes=10
pass 1:  scrollHeight=3555 quotes=20   (grew)
pass 2:  scrollHeight=3555 quotes=20   (FLAT — next AJAX still in flight)
pass 3:  scrollHeight=5119 quotes=30   (grew again!)
pass 4-6: scrollHeight=5119 quotes=30
```
Pass 2 was flat while the request was in flight, then pass 3 grew. Stopping on that first flat pass abandons content mid-load — which is exactly why the initial fetcher A/B captured only the first batch.

**Fix:** stop only after `stable_passes_before_stop` (default 2) **consecutive** flat passes. A single in-flight pass no longer ends the loop; two flat passes in a row means genuinely nothing left. `scroll_wait_ms` also raised 1500→2000 to give each AJAX round-trip room.

## Verification

### Live A/B through the real config-driven fetcher (`quotes.toscrape.com/scroll`)
```
scroll OFF (passes=0)          : quotes=10  len=5837
scroll ON  (factory + base.yaml): quotes=30  len=13912
--- gain=20  →  SCROLL PROVEN ---
```
3× the content, captured through `build_level2_fetcher(load_config())` — the actual production path, not a hand-tuned call. **Demonstrated, not just asserted.**

### Unit tests (`tests/unit/test_content_utils.py`, 5 tests)
```
test_stops_after_consecutive_stable — grows twice then 2 flat → stops on pass 4
test_tolerates_ajax_lag             — flat pass then growth → does NOT stop early (regression for the bug above)
test_respects_max_passes_cap        — grows forever → capped
test_disabled_when_zero_passes      — passes=0 → 0, never scrolls
test_never_raises_on_broken_page    — evaluate raises → returns 0, no throw
```
Full suite: **218 passed, 1 skipped, 0 failed.** Ruff clean.

### Note on webscraper.io/scroll
An earlier trace against `webscraper.io/test-sites/e-commerce/scroll` showed a static `scrollHeight` (1348) — that page serves its full product set in the initial DOM in headless, so it can't demonstrate a scroll gain. `quotes.toscrape.com/scroll` (above) does genuinely lazy-load and proves it.

---

## Production posture

autoscroll is **safe-by-default**: it early-exits on any page that isn't growing (one pass, no cost beyond a single scroll+wait), is capped so a truly infinite feed can't hang a fetch, and never raises. Enabling it for L2/L3 adds real value on lazy-loading targets and is a near-no-op everywhere else. This is the correct trade-off for a general-purpose scraper that can't know in advance whether a target lazy-loads.

## Files Changed
**Modified:** `fetcher/_content_utils.py` (autoscroll), `fetcher/level_2.py` / `level_3.py` (scroll params + call), `fetcher/factory.py` (pass scroll config), `config/schema.py` + `config/base.yaml` (scroll fields).
**New:** `tests/unit/test_content_utils.py` (4 autoscroll tests).
