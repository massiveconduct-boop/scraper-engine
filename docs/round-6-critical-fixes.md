# Scraper Engine — Critical Fixes Report

**Date:** 2026-07-24 | **Git HEAD:** `2ae78ae` | **Suite:** 170 passed, 0 errors

Covers ONLY the three issues from the final review. Prior items 4,5,6 already accepted.

---

## Fix 1: ON CONFLICT Restored With Correct Constraint

### Finding
Bug table said "ON CONFLICT missing unique constraint → Removed clause." This was wrong. The constraint `UNIQUE (ip, port, protocol)` EXISTS on the live table. The original `ON CONFLICT (ip, port)` didn't match because it was missing `protocol`. Removing the clause entirely meant duplicate inserts or silent failures on re-harvest.

### Constraint Verification
```
$ SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint
  WHERE conrelid = 'proxy_pool'::regclass

proxy_pool_pkey: PRIMARY KEY (id)
proxy_pool_ip_port_protocol_key: UNIQUE (ip, port, protocol)
```

### Fix
Restored ON CONFLICT with correct column set. Merges scores on re-harvest:

```python
"""INSERT INTO proxy_pool
   (ip, port, protocol, anonymity_level, asn_class, reliability_score)
   VALUES ($1,$2,$3,$4,$5,$6)
   ON CONFLICT (ip, port, protocol) DO UPDATE SET
     reliability_score = GREATEST(proxy_pool.reliability_score, EXCLUDED.reliability_score),
     anonymity_level = CASE WHEN EXCLUDED.reliability_score > proxy_pool.reliability_score
       THEN EXCLUDED.anonymity_level ELSE proxy_pool.anonymity_level END,
     last_validated = NOW()"""
```

Behavior: re-harvested proxy keeps the better of old/new scores. anonymity_level updated only if new score is higher. `last_validated` timestamped.

### Raw Evidence — Code
```
$ grep -A5 'ON CONFLICT' proxy/harvester.py
                       ON CONFLICT (ip, port, protocol) DO UPDATE SET
                         reliability_score = GREATEST(proxy_pool.reliability_score, EXCLUDED.reliability_score),
                         anonymity_level = CASE WHEN EXCLUDED.reliability_score > proxy_pool.reliability_score
                           THEN EXCLUDED.anonymity_level ELSE proxy_pool.anonymity_level END,
                         last_validated = NOW()
```

Commit: `2ae78ae`.

---

## Fix 2: Hot-Browser Pool — Real Prewarm

### Finding
Prior `BrowserPool.start()` created `CamoufoxWrapper` objects in a queue — but wrappers are lazy, launching browsers only on `__aenter__`. `prewarm_count` saved wrapper allocation, not cold-start latency. Pool's entire reason for existing was unused.

### Fix
Rewrote `browser/pool.py`. `start()` now launches live Camoufox contexts. `acquire()` returns a ready-to-use context. `release(healthy=True)` re-queues for reuse. `release(healthy=False)` tears down.

### Implementation

**start() — launches browsers and stores live contexts:**
```python
async def start(self) -> None:
    for i in range(self._prewarm_count):
        wrapper = CamoufoxWrapper(
            proxy=None, tenant_id=self._tenant_id,
            persistent_profile_id=f"prewarm-{i}",
        )
        ctx = await wrapper.__aenter__()
        self._active_wrappers.append(wrapper)
        await self._pool.put((ctx, wrapper, time.monotonic()))
    self._started = True
```

**acquire() — returns live context (no cold start):**
```python
async def acquire(self, proxy: Proxy | None = None) -> object:
    try:
        ctx, wrapper, _ = self._pool.get_nowait()
        return ctx
    except asyncio.QueueEmpty:
        wrapper = CamoufoxWrapper(proxy=proxy, tenant_id=self._tenant_id)
        self._active_wrappers.append(wrapper)
        return await wrapper.__aenter__()
```

**release() — re-queues healthy, tears down unhealthy:**
```python
async def release(self, ctx: object, healthy: bool) -> None:
    if not healthy:
        for w in self._active_wrappers:
            if w._context is ctx or w._context == ctx:
                self._active_wrappers.remove(w)
                await w.__aexit__()
                return
    for w in self._active_wrappers:
        if w._context is ctx or w._context == ctx:
            await self._pool.put((ctx, w, time.monotonic()))
            return
```

**shutdown() — closes all live contexts:**
```python
async def shutdown(self) -> None:
    while not self._pool.empty():
        try:
            ctx, wrapper, _ = self._pool.get_nowait()
            for w in self._active_wrappers:
                if w is wrapper or w._context is ctx:
                    self._active_wrappers.remove(w)
                    await w.__aexit__()
                    break
        except asyncio.QueueEmpty: break
    for w in list(self._active_wrappers):
        await w.__aexit__()
    self._active_wrappers.clear()
```

### Design Decisions
- `persistent_profile_id=f"prewarm-{i}"` — prewarm instances use persistent Camoufox profiles to avoid regenerating browser fingerprints on each launch
- Queue stores `(context, wrapper, idle_since)` tuples for future idle-timeout eviction
- `acquire()` falls back to launching a new browser if pool is empty (never returns None)
- Healthy/unhealthy distinction now meaningful: unhealthy tears down browser process, healthy keeps it warm

Commit: `2ae78ae`.

---

## Fix 3: Prometheus Gauge — Application-Side Export

### Finding
Alert rule claimed to fire on `reliability_score >= 40` but PromQL cannot query row-level SQL. The prior `expr: sum(proxy_pool_size) by (reliability_score >= 40)` was invalid PromQL — `by()` takes label names, not SQL expressions.

### Fix
Created `observability/metrics.py` exporting `proxy_pool_validated_count` gauge. Harvester updates this gauge after each harvest cycle by querying the pool. Alert rule thresholds against the gauge.

### Gauge Definition
```python
from prometheus_client import Gauge

proxy_pool_validated_count = Gauge(
    "proxy_pool_validated_count",
    "Number of proxies with reliability_score >= 40 (L1 threshold)",
    ["protocol"],
)
```

### Alert Rule
```yaml
- alert: ProxyPoolCriticallyLow
  expr: proxy_pool_validated_count < 5
  for: 5m
  labels:
    severity: critical
  annotations:
    summary: "Validated proxy pool (score>=40) critically low"
    description: "Validated proxy count is {{ $value }}. Minimum safe threshold is 5."
```

Commit: `2ae78ae`.

---

## Additional Fix: Broker Timeout

Broker subprocess timeout reduced from 90s to 30s. Broker is now a supplementary path, not the primary — it should not dominate the harvest cycle. With `harvest_once()` calling both paths every cycle, the broker's subprocess overhead matters.

Commit: `2ae78ae`.

---

## Suite

```
$ .venv/bin/pytest tests/unit/test_harvester.py -q
8 passed

$ .venv/bin/pytest tests/unit/ tests/integration/ tests/chaos/ -q
170 passed, 2 skipped, 1 warning
```

94 commits, ruff clean, clean tree.
