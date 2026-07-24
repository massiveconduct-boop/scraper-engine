# Scraper Engine — Double-Issue Bug Fix Report

**Date:** 2026-07-24 | **Spec:** `specs/scraper-engine-blueprint-v2.md` v2.0

Covers ONLY the `acquire()` double-issue bug flagged in the last review.

---

## Finding: `acquire()` Double-Issues Live Browser Contexts

### Root Cause
`acquire()` drained `self._pool` into a local list, immediately re-queued ALL non-expired items back to `self._pool`, then returned ONE item from the local list. The returned item was never removed from `self._pool`. A second `acquire()` call found the identical context still in the queue and handed it to a second caller.

Two callers driving the same Camoufox page object simultaneously — cross-request interference on a shared resource.

### Bug Trace (Before Fix)
```
1. pool = [ctx_A, ctx_B]
2. fresh = [ctx_A, ctx_B]; pool = []          # drain
3. pool = [ctx_A, ctx_B]                       # re-queue ALL non-expired
4. return ctx_A                                 # ctx_A still in pool!
5. Second call: pool.get_nowait() → ctx_A      # SAME context handed out again
```

---

## Fix

Every drained item classified exactly once: selected, kept, or torn down. Only `keep` items go back into `self._pool`. Selected item never re-queued.

### Current `acquire()` (Fixed)

```python
    async def acquire(self, proxy: Proxy | None = None, domain: str | None = None) -> object:
        """Get a live browser context from the pool or launch a new one.

        Drains pool once, classifies each candidate as selected/keep/
        teardown. Only keepers go back into the pool. Nothing is ever
        simultaneously returned AND still queued — prevents double-issue.
        """
        now = time.monotonic()
        drained = []
        while not self._pool.empty():
            try:
                drained.append(self._pool.get_nowait())
            except asyncio.QueueEmpty:
                break

        selected = None
        keep = []
        for ctx, wrapper, idle_since in drained:
            # Idle timeout — tear down
            if now - idle_since > self._max_idle_seconds:
                for w in self._active_wrappers:
                    if w is wrapper or w._context is ctx:
                        self._active_wrappers.remove(w)
                        await w.__aexit__()
                        break
                continue
            # Domain mismatch — tear down (stale cookies flushed)
            if domain is not None and getattr(wrapper, '_last_domain', None) != domain:
                for w in self._active_wrappers:
                    if w is wrapper or w._context is ctx:
                        self._active_wrappers.remove(w)
                        await w.__aexit__()
                        break
                continue
            # First candidate (no domain) or domain match — select
            if selected is None:
                selected = (ctx, wrapper)
            else:
                keep.append((ctx, wrapper, idle_since))

        for item in keep:
            await self._pool.put(item)

        if selected is not None:
            return selected[0]

        wrapper = CamoufoxWrapper(
            proxy=proxy,
            tenant_id=self._tenant_id,
        )
        self._active_wrappers.append(wrapper)
        return await wrapper.__aenter__()
```

---

## Regression Tests

Two unit tests added — no Camoufox runtime needed. Mock wrapper/context objects catch the double-issue.

### Raw Output
```
$ .venv/bin/pytest tests/unit/test_browser.py::TestAcquireDoubleIssue -v

test_two_sequential_acquires_get_different_contexts PASSED
test_three_sequential_all_different PASSED
2 passed in 0.12s
```

### Test: Two Sequential Acquires Get Different Contexts
```python
    async def test_two_sequential_acquires_get_different_contexts(self):
        """Two sequential acquire() calls with a queued context must not
        return the same object. Verbatim regression test for the double-issue
        bug where acquire() re-queued items to self._pool before selecting,
        leaving the selected item still in the queue for the next call."""
        pool = BrowserPool(tenant_id=TenantId("doubletest"), prewarm_count=0)
        await pool.start()

        # Inject a fake live context into the pool (simulating prewarm)
        fake_ctx = object()
        fake_wrapper = MagicMock()
        fake_wrapper._last_domain = None
        await pool._pool.put((fake_ctx, fake_wrapper, asyncio.get_event_loop().time()))

        # First acquire — should get the fake context
        ctx1 = await pool.acquire()
        assert ctx1 is fake_ctx, "first acquire should return the queued context"

        # Second acquire — pool should be empty, must NOT return same ctx
        with patch.object(pool, '_active_wrappers', []), \
             patch('browser.pool.CamoufoxWrapper') as mock_cw:
            def make_mock(*a, **kw):
                inst = MagicMock()
                inst.__aenter__ = AsyncMock(return_value=object())
                return inst
            mock_cw.side_effect = make_mock
            ctx2 = await pool.acquire()
            assert ctx2 is not fake_ctx, (
                "DOUBLE-ISSUE BUG: second acquire returned same context. "
                "The item was selected but never removed from self._pool."
            )
```

### Test: Three Sequential All Different
```python
    async def test_three_sequential_all_different(self):
        """Three acquires with one pre-loaded context: first gets it, rest launch fresh."""
        pool = BrowserPool(tenant_id=TenantId("tripletest"), prewarm_count=0)
        await pool.start()

        fake_ctx = object()
        fake_wrapper = MagicMock()
        fake_wrapper._last_domain = None
        await pool._pool.put((fake_ctx, fake_wrapper, asyncio.get_event_loop().time()))

        ctx1 = await pool.acquire()
        assert ctx1 is fake_ctx

        with patch.object(pool, '_active_wrappers', []), \
             patch('browser.pool.CamoufoxWrapper') as mock_cw:
            def make_mock(*a, **kw):
                inst = MagicMock()
                inst.__aenter__ = AsyncMock(return_value=object())
                return inst
            mock_cw.side_effect = make_mock

            ctx2 = await pool.acquire()
            ctx3 = await pool.acquire()
            assert ctx2 is not fake_ctx
            assert ctx3 is not fake_ctx
            assert ctx2 is not ctx3, "two sequential launches must create distinct contexts"
```

---

## Status

| Item | Status | Evidence |
|---|---|---|
| Double-issue bug | **FIXED** | acquire() rewrite + 2 regression tests PASS |
| Regression tests | **2 passed** | Raw pytest output, 0.12s |
| Domain guard | **Bundled** | Domain mismatch teardown in same classify loop |
| Idle timeout | **Bundled** | Expiry teardown in same classify loop |

Ruff clean. No Camoufox required for these tests.

```
$ .venv/bin/ruff check browser/pool.py
[1;32mAll checks passed![0m
```
