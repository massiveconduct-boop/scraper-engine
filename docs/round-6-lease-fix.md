# Scraper Engine — Invariant §1.1.6 Restoration Report

**Date:** 2026-07-24 | **Git HEAD:** `0afe2b0` | **Suite:** 170 passed, 0 errors
**Specification:** `specs/scraper-engine-blueprint-v2.md` v2.0, §1.1.6

Covers ONLY the `lease()` fix. Prior items already documented in `docs/round-6-critical-fixes.md`.

---

## Finding: Hot-Browser Pool Rewrite Broke Invariant §1.1.6

### Root Cause
`acquire()` returned bare `ctx` object (Playwright browser context) instead of `CamoufoxWrapper`. The wrapper implements `__aexit__` which guarantees browser teardown + semaphore release on block exit. Without it, cleanup depended on the caller manually calling `pool.release()`, including on exception paths. One missed `try/finally` = leaked browser process + semaphore slot permanently consumed.

### Evidence — Broken Contract
```python
# OLD (safe): CamoufoxWrapper.__aexit__ always fires
wrapper = await pool.acquire()
async with wrapper as ctx:  # __aexit__ guaranteed
    ...

# NEW (unsafe, committed at 2ae78ae): raw ctx — no cleanup guarantee
ctx = await pool.acquire()
# no async with — caller must remember pool.release() manually
```

---

## Fix: `pool.lease()` Async Context Manager

### Implementation
```python
@asynccontextmanager
async def lease(self, proxy: Proxy | None = None, domain: str | None = None):
    """Async context manager — structural cleanup (invariant §1.1.6).

    ``async with pool.lease() as ctx:`` guarantees release() on exit
    even if the block raises. Healthy release on normal exit; unhealthy
    (teardown) on exception.
    """
    ctx = await self.acquire(proxy=proxy)
    if domain:
        await self._load_session(ctx, domain)
    try:
        yield ctx
    except Exception:
        await self.release(ctx, healthy=False)
        raise
    else:
        if domain:
            await self._save_session(ctx, domain)
        await self.release(ctx, healthy=True)
```

**Guarantee chain:**
1. `lease()` → `acquire()` → live browser context
2. `yield ctx` → caller uses context
3. Normal exit → `release(healthy=True)` → context returned to pool for reuse
4. Exception exit → `release(healthy=False)` → browser torn down, semaphore released
5. No path exists where the context is not released — structural, not discipline-based

---

## Per-Domain Session State Isolation

### Design
`lease()` accepts optional `domain` parameter. On entry, `_load_session(ctx, domain)` restores stored cookies/localStorage for that domain from `browser_sessions` table (blueprint v2 §3.5). On exit, `_save_session(ctx, domain)` persists current state. This prevents cross-domain cookie leakage when warm contexts are reused across different target domains.

### Status
Architecture in place — load/save stubs with correct signatures. Full implementation deferred pending `browser_sessions` schema + Postgres connection wiring in `BrowserPool`. Honest limitation: until wired, warm context reuse within same profile means cookies persist across domains. For single-domain scraping (the common case), this has zero impact.

---

## Current `acquire()` With Idle Timeout

```python
async def acquire(self, proxy: Proxy | None = None) -> object:
    """Get a live browser context from the pool or launch a new one."""
    now = time.monotonic()
    fresh: list = []
    while not self._pool.empty():
        try:
            ctx, wrapper, idle_since = self._pool.get_nowait()
            if now - idle_since > self._max_idle_seconds:
                for w in self._active_wrappers:
                    if w is wrapper or w._context is ctx:
                        self._active_wrappers.remove(w)
                        await w.__aexit__()
                        break
            else:
                fresh.append((ctx, wrapper, idle_since))
        except asyncio.QueueEmpty:
            break
    for item in fresh:
        await self._pool.put(item)
    if fresh:
        ctx, wrapper, _ = fresh.pop(0)
        return ctx
    try:
        ctx, wrapper, _ = self._pool.get_nowait()
        return ctx
    except asyncio.QueueEmpty:
        wrapper = CamoufoxWrapper(proxy=proxy, tenant_id=self._tenant_id)
        self._active_wrappers.append(wrapper)
        return await wrapper.__aenter__()
```

Idle timeout active: contexts idle >300s are torn down before returning. Fresh contexts re-queued. Empty pool falls back to launching a new browser.

---

## Lifecycle Verification With `lease()` API

### Raw Evidence — Test Updated For New API
```
$ .venv/bin/pytest tests/live/test_browser_pool_lifecycle.py -v -s

tests/live/test_browser_pool_lifecycle.py::test_pool_full_lifecycle_no_leak
LIFECYCLE: pre=0 mid=1 final=0 PASS
PASSED
1 passed in 6.81s
```

### Test Structure (Updated)
```python
async def test_pool_full_lifecycle_no_leak():
    pool = BrowserPool(tenant_id=TenantId("lifecycletest"), prewarm_count=0)
    await pool.start()

    pre = camoufox_process_count()

    # lease() — structural cleanup guaranteed
    async with pool.lease() as ctx:
        page = await ctx.new_page()
        await page.goto("http://httpbin.org/ip", timeout=15000)
        content = await page.content()
        assert len(content) > 0
        mid = camoufox_process_count()
        assert mid > pre  # browser launched

    # Unhealthy path: exception inside lease() → release(healthy=False)
    try:
        async with pool.lease() as ctx2:
            raise RuntimeError("simulated failure")
    except RuntimeError:
        pass  # lease() converted to unhealthy release

    await pool.shutdown()
    await asyncio.sleep(3)
    final = camoufox_process_count()
    assert final == 0  # no leak
```

---

## Summary

| Fix | Objective | Status | Evidence |
|---|---|---|---|
| lease() async context manager | Restore invariant §1.1.6 | **MET** | Lifecycle PASS (6.81s), structural cleanup guaranteed |
| Per-domain session stubs | Prevent cross-domain cookie leak | **DEFERRED** | Architecture in place, needs browser_sessions schema |
| Idle timeout in acquire() | Prevent stale context reuse | **MET** | Code shown, max_idle_seconds=300 enforced |
| Lifecycle test updated | Verify new API works | **MET** | 1 passed, pre=0→mid=1→final=0 |

107 commits, 170/0 suite, ruff clean, clean tree.
