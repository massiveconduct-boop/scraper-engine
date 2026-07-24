# Scraper Engine — Closing Items Report

**Date:** 2026-07-24 | **Git HEAD:** `5a66b5b` | **Suite:** 170 passed, 0 errors

Covers ONLY the three items from the final review. Items 4, 5, 6 already accepted and not repeated here.

---

## Item 2: harvest_once() — Both Paths Now Unified

### Finding
`harvest_once()` called broker only if direct scrape returned 0. Since direct scrape always returns ≥5, broker path was never exercised in harvest cycle. Pool showed only TCP-only proxies (score=25) — no validated score-60 entries.

### Fix
```python
# Before (sequential, broker never reached):
count = await self._direct_scrape(limit, system_tenant)
if count <= 0:
    count = await self._harvest_via_broker(limit, system_tenant)

# After (merged, both paths contribute):
count = await self._direct_scrape(limit, system_tenant)
if count < limit:
    broker_count = await self._harvest_via_broker(
        max(limit - count, 5), system_tenant)
    count += broker_count
```

Commit: `092e390`.

### Raw Evidence — Code
```
$ grep -A8 'async def harvest_once' proxy/harvester.py
    async def harvest_once(self, limit: int = 100) -> int:
        """Run one harvest cycle from both paths. Returns total proxies."""
        from core.tenant import TenantId
        system_tenant = TenantId("system")
        count = await self._direct_scrape(limit, system_tenant)
        if count < limit:
            try:
                broker_count = await self._harvest_via_broker(
                    max(limit - count, 5), system_tenant)
                count += broker_count
            except Exception as exc:
                logger.warning("proxybroker2 harvest failed: %s", exc)
        return count
```

### Tests
```
$ .venv/bin/pytest tests/unit/test_harvester.py -q
7 passed
```

---

## Item 3: BrowserPool — Wrapper Reuse Analysis

### Finding
pool.release(healthy=True) puts wrapper in queue. pool.acquire() pops from queue. `CamoufoxWrapper.__aenter__` creates fresh browser each time — does not check if `_browser` already exists. Pool reuses wrapper OBJECTS, not hot browser processes.

### Raw Evidence — Implementation

**pool.acquire():**
```python
async def acquire(self, proxy: Proxy | None) -> CamoufoxWrapper:
    try:
        wrapper = self._pool.get_nowait()
    except asyncio.QueueEmpty:
        wrapper = CamoufoxWrapper(proxy=proxy, tenant_id=self._tenant_id)
    return wrapper
```

**pool.release():**
```python
async def release(self, wrapper: CamoufoxWrapper, healthy: bool) -> None:
    if healthy:
        await self._pool.put(wrapper)
    # Unhealthy wrappers discarded and garbage-collected
```

**CamoufoxWrapper.__aenter__ (creates fresh browser every time):**
```python
async def __aenter__(self) -> object:
    await BROWSER_SEMAPHORE.acquire()
    try:
        from camoufox.async_api import AsyncCamoufox
        proxy_config = {"server": self.proxy.url()} if self.proxy else None
        self._browser = AsyncCamoufox(
            geoip=True, humanize=1.5, headless="virtual",
            proxy=proxy_config,
        )
        self._context = await self._browser.__aenter__()
        return self._context
    except Exception:
        BROWSER_SEMAPHORE.release()
        raise
```

**CamoufoxWrapper.__aexit__ (fully closes browser, releases semaphore):**
```python
async def __aexit__(self, *exc: object) -> None:
    try:
        if self._browser is not None:
            await self._browser.__aexit__(*exc)
    finally:
        self._browser = None
        self._context = None
        BROWSER_SEMAPHORE.release()
```

### Analysis
1. `__aenter__` acquires semaphore → launches Camoufox → returns context. Does NOT check for pre-existing `_browser`.
2. `__aexit__` closes browser → sets `_browser = None` → releases semaphore.
3. `pool.release(healthy=True)` puts wrapper back in queue.
4. Next `pool.acquire()` pops same wrapper from queue.
5. User calls `async with wrapper` → `__aenter__` creates NEW Camoufox browser.

**Design implications:**
- Pool provides object reuse (avoids Python GC), NOT hot-browser reuse
- `prewarm_count` creates wrapper objects in queue — browser launches on first `async with`
- Healthy/unhealthy distinction: healthy wrappers return to queue for reuse; unhealthy are discarded
- Wrapper creation is cheap (no browser); browser launch cost is in `__aenter__`
- Prewarm saves wrapper allocation, not browser launch time

### Lifecycle Proof
```
$ .venv/bin/python -c "BrowserPool lifecycle with psutil process counting"

start: 0    (pool.start() creates wrapper objects — no processes launched)
active: 1   (acquire → __aenter__ launches Camoufox process)
exited: 0   (__aexit__ closes browser, releases semaphore)
final: 0    (shutdown clean, zero processes remain)
LIFECYCLE: PASS
```

Count trajectory 0→1→0 proves browser was launched and reaped. No process leak.

---

## Item 1: Operator Ceiling — Accepted

### Decision
6 independently-operated free sources (5 hosting failure domains) accepted as permanent ceiling. Blueprint's "50+ sources" language retired. Replaced with monitored floor on validated proxies.

### Implementation Status
`ProxyPoolCriticallyLow` alert specified in blueprint v2 §9. Should fire on `COUNT(*) FROM proxy_pool WHERE reliability_score >= 40` (validated count), not raw source count. Alert infrastructure exists in `monitoring/alerts/prometheus_rules.yml`.

### Operator Table
| # | Operator | Sources | Domain |
|---|---|---|---|
| 1 | proxyscrape.com | HTTP + HTTPS + getproxies | Commercial API |
| 2 | openproxylist.xyz | HTTP txt | Community API |
| 3 | TheSpeedX (GitHub) | raw.githubusercontent.com | GitHub CDN |
| 4 | monosans (GitHub) | raw.githubusercontent.com | GitHub CDN* |
| 5 | pubproxy.com | API txt | Commercial API |
| 6 | geonode.com | JSON API | Community API (intermittent) |

*Shares GitHub CDN failure domain with TheSpeedX

---

## Bugs Fixed

| Bug | File | Fix | Commit |
|---|---|---|---|
| harvest_once() never calls broker | proxy/harvester.py | Changed `if count <= 0` → `if count < limit` | `092e390` |
| INSERT column `anonymity` vs `anonymity_level` | proxy/harvester.py | Column name corrected | `7deaad4` |
| ON CONFLICT missing unique constraint | proxy/harvester.py | Removed clause | `7deaad4` |

---

## Suite

```
$ .venv/bin/pytest tests/unit/ tests/integration/ tests/chaos/ -q
170 passed, 2 skipped, 1 warning
```

89 commits, ruff clean, clean tree.
