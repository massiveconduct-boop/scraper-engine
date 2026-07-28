# Round 7 Implementation Plan — Alert Wiring, Proxy Promotion, Session Isolation
### Code-Level Specification. Each Item Has an Exact Exit Condition and Required Evidence.

**Implementation order matters — do them in this sequence, not in parallel:**
1. Session isolation first — it touches `browser/pool.py`, the file that just had a
   correctness bug fixed and regression-tested. Anything else landing in that file
   before this is done risks re-breaking `acquire()`.
2. Background promotion second — it's isolated to `proxy/harvester.py` and the DB,
   no shared-file risk with anything else.
3. Alert wiring last — it depends on nothing code-side changing; it's infra/config,
   and doing it last means there's more real signal (session bugs, promotion
   failures) to prove the alert pipeline actually catches something real, not just a
   synthetic test trigger.

---

## Item 5 — Session Isolation (do first)

### 5.0 Decision Point — Confirm Before Writing Code

`CamoufoxWrapper.__aenter__` currently constructs `AsyncCamoufox(...)` and returns
its context with no hook for pre-loading state. There are two possible ways to load
a persisted session into a **freshly launched** context, and which one is correct
depends on whether the real `camoufox` library's `AsyncCamoufox`/context-creation
call accepts and forwards a `storage_state` kwarg the way Playwright's
`browser.new_context(storage_state=...)` does.

**Check this first, five minutes, before writing anything else:**
```python
import inspect
from camoufox.async_api import AsyncCamoufox
print(inspect.signature(AsyncCamoufox.__init__))
```
- **If `storage_state` (or an equivalent passthrough `**kwargs` to Playwright's
  context creation) is accepted:** use Path A below (native, correct, handles
  cookies + localStorage + sessionStorage in one call).
- **If not:** use Path B below (manual cookie injection + per-origin localStorage
  script injection) — more code, but works with any Playwright-based context
  regardless of what Camoufox exposes.

Report back which path applies before continuing — don't guess and ship the wrong
one; they're not interchangeable and the wrong one will silently fail (empty
`storage_state` kwarg gets ignored by most `**kwargs`-swallowing constructors
without an error, so this fails silently, not loudly).

### 5.1 Database — Confirm/Migrate `browser_sessions`

Already in blueprint v2 §5, per-tenant schema. Confirm it matches exactly (add
migration if any column is missing):

```sql
CREATE TABLE IF NOT EXISTS browser_sessions (
    session_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    domain VARCHAR(255) NOT NULL,
    storage_state JSONB NOT NULL,
    last_used_at TIMESTAMPTZ DEFAULT NOW(),
    expires_at TIMESTAMPTZ NOT NULL DEFAULT (NOW() + INTERVAL '30 days'),
    UNIQUE (domain)
);
CREATE INDEX IF NOT EXISTS idx_sessions_expiry ON browser_sessions (expires_at);
```
Table lives inside the per-tenant schema, so `domain` alone is a safe unique key
within it — no `tenant_id` column needed, `search_path` already scopes it (per
blueprint §3.10's `PostgresClient.acquire(tenant_id)` pattern — reuse it here, don't
open a second, unscoped connection path for this feature).

### 5.2 `browser/session_state.py` — Real Implementation

Round 5 reported 3 passing tests for this module — check now whether those tests
run against a real Postgres connection or an in-memory/mock stand-in. If mock: this
is unwired, exactly the state Item 4's proxy-promotion "job exists but isn't
scheduled" is in — code present, never actually connected. Fix that here.

```python
# browser/session_state.py
import json
from datetime import datetime, timedelta
from core.tenant import TenantId
from storage.postgres_client import PostgresClient

class SessionStateManager:
    def __init__(self, pg: PostgresClient, ttl_days: int = 30):
        self._pg = pg
        self._ttl_days = ttl_days

    async def load(self, tenant_id: TenantId, domain: str) -> dict | None:
        async with self._pg.acquire(tenant_id) as conn:
            row = await conn.fetchrow(
                "SELECT storage_state FROM browser_sessions "
                "WHERE domain = $1 AND expires_at > NOW()",
                domain,
            )
        if row is None:
            return None
        return json.loads(row["storage_state"]) if isinstance(row["storage_state"], str) else row["storage_state"]

    async def save(self, tenant_id: TenantId, domain: str, storage_state: dict) -> None:
        expires_at = datetime.utcnow() + timedelta(days=self._ttl_days)
        async with self._pg.acquire(tenant_id) as conn:
            await conn.execute(
                """INSERT INTO browser_sessions (domain, storage_state, last_used_at, expires_at)
                   VALUES ($1, $2, NOW(), $3)
                   ON CONFLICT (domain) DO UPDATE SET
                     storage_state = EXCLUDED.storage_state,
                     last_used_at = NOW(),
                     expires_at = EXCLUDED.expires_at""",
                domain, json.dumps(storage_state), expires_at,
            )

    async def delete(self, tenant_id: TenantId, domain: str) -> None:
        """Called when a session turns out to be bad (e.g. site invalidated it,
        got logged out) — don't keep reloading a poisoned session forever."""
        async with self._pg.acquire(tenant_id) as conn:
            await conn.execute("DELETE FROM browser_sessions WHERE domain = $1", domain)
```

### 5.3a — Path A: Native `storage_state` Passthrough

**`browser/camoufox_wrapper.py`:**
```python
async def __aenter__(self, storage_state: dict | None = None) -> object:
    await BROWSER_SEMAPHORE.acquire()
    try:
        from camoufox.async_api import AsyncCamoufox
        proxy_config = {"server": self.proxy.url()} if self.proxy else None
        kwargs = dict(geoip=True, humanize=1.5, headless="virtual", proxy=proxy_config)
        if storage_state is not None:
            kwargs["storage_state"] = storage_state   # confirmed accepted per 5.0 check
        self._browser = AsyncCamoufox(**kwargs)
        self._context = await self._browser.__aenter__()
        return self._context
    except Exception:
        BROWSER_SEMAPHORE.release()
        raise
```
Note the signature change — `__aenter__` doesn't normally take arguments (Python's
`async with` protocol calls it with none). Since `CamoufoxWrapper` is constructed
fresh per-launch anyway, pass `storage_state` through the **constructor** instead,
not `__aenter__`:
```python
def __init__(self, proxy, tenant_id, persistent_profile_id=None, storage_state=None):
    ...
    self._storage_state = storage_state

async def __aenter__(self) -> object:
    ...
    if self._storage_state is not None:
        kwargs["storage_state"] = self._storage_state
    ...
```

### 5.3b — Path B: Manual Cookie + localStorage Injection (if Path A unavailable)

```python
async def _apply_storage_state(ctx, storage_state: dict) -> None:
    cookies = storage_state.get("cookies", [])
    if cookies:
        await ctx.add_cookies(cookies)
    for origin_entry in storage_state.get("origins", []):
        origin = origin_entry["origin"]
        page = await ctx.new_page()
        try:
            await page.goto(origin, wait_until="domcontentloaded", timeout=10000)
            for item in origin_entry.get("localStorage", []):
                await page.evaluate(
                    "([k, v]) => window.localStorage.setItem(k, v)",
                    [item["name"], item["value"]],
                )
        finally:
            await page.close()

async def _capture_storage_state(ctx) -> dict:
    # Playwright's native context.storage_state() works regardless of Path A/B —
    # it's a read, not a context-creation-time write, so this half is always native.
    return await ctx.storage_state()
```
Call `_apply_storage_state` right after context creation in `__aenter__` (Path B
variant), before returning the context to the pool/caller.

### 5.4 `browser/pool.py` — Wire Load/Save Into the Already-Fixed `acquire()`/`lease()`

**Critical constraint: do not touch the classify-loop structure from the double-issue
fix.** Session load/save happens *outside* that loop, in two specific places:

1. **On domain-miss fresh launch** (the branch in `acquire()` that currently does
   `wrapper = CamoufoxWrapper(proxy=proxy, tenant_id=self._tenant_id)` after finding
   no matching warm context) — load the persisted session first, pass it into the
   constructor:
   ```python
   # inside acquire(), in the "no match found, launch fresh" branch:
   session_state = None
   if domain is not None:
       session_state = await self._session_manager.load(self._tenant_id, domain)
   wrapper = CamoufoxWrapper(
       proxy=proxy, tenant_id=self._tenant_id, storage_state=session_state,
   )
   self._active_wrappers.append(wrapper)
   ctx = await wrapper.__aenter__()
   wrapper._last_domain = domain   # tag immediately, not just on release
   return ctx
   ```

2. **On healthy release, inside `lease()`'s exit path** — save state back:
   ```python
   # in pool.lease()'s __aexit__ / release(healthy=True) path:
   if healthy and domain is not None:
       state = await wrapper.get_context().storage_state()
       await self._session_manager.save(self._tenant_id, domain, state)
   ```

Do **not** save/load inside the classify loop that iterates `drained` — that loop's
entire job is queue bookkeeping (select/keep/teardown) and must stay exactly as
narrow as the double-issue fix left it. Session I/O is a property of the
launch/release boundary, not the queue-scan.

### 5.5 Required Evidence (do not accept "code exists" as done — this bit the
project on Item 4 already)

```python
# tests/unit/test_session_isolation.py — no Camoufox needed, mock the wrapper
async def test_domain_a_then_domain_b_does_not_carry_cookies():
    """Regression test for the original leak: acquire for domain A, simulate a
    cookie being set, release healthy, acquire for domain B, assert B's context
    does NOT contain A's cookie."""
    ...

# tests/live/test_session_persistence.py — requires Camoufox + Postgres
async def test_session_survives_pool_recycle():
    """Acquire for a real domain, set a distinctive cookie via page.evaluate,
    release healthy, force the warm context out (release a second, different-
    domain acquire to trigger teardown per the domain guard), acquire the ORIGINAL
    domain again -> assert the distinctive cookie is present (loaded from DB, not
    from a surviving warm context, since the warm one was just torn down)."""
```
The second test is the one that actually proves persistence works — the first only
proves isolation. Both are required; isolation without persistence just means
nothing leaks because nothing loads either.

**Evidence required in the report:** raw pytest output for both tests, plus one
manual end-to-end run showing a `SELECT domain, expires_at FROM browser_sessions;`
row appearing after a live scrape, with the row's `domain` matching the scraped
target.

---

## Item 4 — Background Proxy Promotion

### 4.1 Schema Migration

```sql
ALTER TABLE proxy_pool ADD COLUMN IF NOT EXISTS promotion_attempts INTEGER NOT NULL DEFAULT 0;
ALTER TABLE proxy_pool ADD COLUMN IF NOT EXISTS last_promotion_attempt_at TIMESTAMPTZ;
```
Without an attempt counter, a proxy that will never work (e.g. a dead datacenter IP
that happens to still accept TCP connects) gets re-validated forever, every cycle,
at zero benefit and real cost. Cap it.

### 4.2 The Job

```python
# proxy/promotion.py
import asyncio
import logging
from core.tenant import TenantId
from proxy.harvester import ProxyHarvester  # reuse _http_validate, don't duplicate it

logger = logging.getLogger(__name__)

MAX_PROMOTION_ATTEMPTS = 5
PROMOTION_BATCH_SIZE = 20       # bound per cycle — don't try to re-validate the whole tcp-only tier at once
PROMOTION_CONCURRENCY = 5        # bounded parallel HTTP validations, same spirit as BROWSER_SEMAPHORE

class ProxyPromotionJob:
    def __init__(self, pg, http_validate_fn, system_tenant: TenantId = TenantId("system")):
        self._pg = pg
        self._http_validate = http_validate_fn   # ProxyHarvester._http_validate, injected not imported-and-coupled
        self._tenant = system_tenant
        self._sem = asyncio.Semaphore(PROMOTION_CONCURRENCY)

    async def run_once(self) -> dict:
        async with self._pg.acquire(self._tenant) as conn:
            candidates = await conn.fetch(
                """SELECT id, ip, port, protocol FROM proxy_pool
                   WHERE reliability_score < 40
                     AND promotion_attempts < $1
                     AND (last_promotion_attempt_at IS NULL
                          OR last_promotion_attempt_at < NOW() - INTERVAL '15 minutes')
                   ORDER BY last_promotion_attempt_at ASC NULLS FIRST
                   LIMIT $2""",
                MAX_PROMOTION_ATTEMPTS, PROMOTION_BATCH_SIZE,
            )

        promoted, failed, exhausted = 0, 0, 0

        async def _try_one(row):
            nonlocal promoted, failed, exhausted
            async with self._sem:
                is_valid, anonymity = await self._http_validate(row["ip"], row["port"], row["protocol"])
            async with self._pg.acquire(self._tenant) as conn:
                if is_valid:
                    await conn.execute(
                        """UPDATE proxy_pool SET reliability_score = 60, anonymity_level = $1,
                           last_promotion_attempt_at = NOW() WHERE id = $2""",
                        anonymity.value, row["id"],
                    )
                    promoted += 1
                else:
                    new_attempts = row.get("promotion_attempts", 0) + 1  # or fetch fresh; keep it simple, single-writer per row
                    await conn.execute(
                        """UPDATE proxy_pool SET promotion_attempts = promotion_attempts + 1,
                           last_promotion_attempt_at = NOW() WHERE id = $1""",
                        row["id"],
                    )
                    failed += 1
                    if new_attempts >= MAX_PROMOTION_ATTEMPTS:
                        exhausted += 1

        await asyncio.gather(*[_try_one(row) for row in candidates])
        logger.info("promotion cycle: %d candidates, %d promoted, %d failed, %d exhausted",
                    len(candidates), promoted, failed, exhausted)
        return {"candidates": len(candidates), "promoted": promoted, "failed": failed, "exhausted": exhausted}

    async def run_forever(self, interval_seconds: int = 900):
        while True:
            try:
                await self.run_once()
            except Exception:
                logger.exception("promotion cycle failed, will retry next interval")
            await asyncio.sleep(interval_seconds)
```

**Exhausted proxies (`promotion_attempts >= 5`) are not deleted** — left at
`reliability_score < 40`, permanently unselectable, but retained for audit/debugging
(you can always see how many dead proxies came from which source, which is useful
signal for Item 1's source-quality tracking). If table growth becomes a real
concern, add a separate pruning job later — don't conflate "stop retrying" with
"delete," they're different decisions.

### 4.3 Scheduling — Same Process, Same Event Loop as the Harvester

`proxy-harvester` already runs standalone (`command: python -m proxy.harvester`,
confirmed in the Item-4 diagnostic report). Add the promotion loop as a second
concurrent task in that same process's entrypoint — don't stand up a second
container/process for this:

```python
# proxy/__main__.py (or wherever the harvester's entrypoint currently lives)
async def main():
    harvester = ProxyHarvester(...)
    promotion = ProxyPromotionJob(pg=harvester._pg, http_validate_fn=harvester._http_validate)
    await asyncio.gather(
        harvester.run_forever(interval_seconds=600),
        promotion.run_forever(interval_seconds=900),
    )
```

### 4.4 Required Evidence

```sql
-- Before a promotion cycle:
SELECT COUNT(*), AVG(promotion_attempts) FROM proxy_pool WHERE reliability_score < 40;
-- Run one promotion cycle manually:
```
```python
result = await promotion.run_once()
print(result)  # {"candidates": N, "promoted": N, "failed": N, "exhausted": N}
```
```sql
-- After: confirm at least the promoted count moved
SELECT reliability_score, COUNT(*) FROM proxy_pool GROUP BY reliability_score;
```
Given free-proxy HTTP-forwarding rates already measured at ~0.02% in this project's
own evidence, **do not require a nonzero `promoted` count as the pass condition** —
require that `run_once()` executes cleanly, updates `last_promotion_attempt_at` and
`promotion_attempts` correctly (verifiable even with `promoted=0`), and that a
proxy manually seeded with a known-working local judge (point one test proxy at
`judge_server.py` itself, or a proxy you control) does get promoted to score 60 —
that's the actual correctness proof, not relying on the wild internet cooperating
during the report window.

---

## Item 3 — Alert Wiring

### 3.0 Decision Point — Where Does The Page Actually Go?

Nothing in any report so far has specified a paging destination. Pick one before
implementing — the config below defaults to a **Slack incoming webhook** because
it's free, takes five minutes to set up, and needs no additional account, but swap
for PagerDuty/Opsgenie if there's an on-call rotation this should actually
interrupt someone's sleep for. This is a product-owner decision, not an engineering
one — but don't leave it unmade the way the proxy-source-count question almost
stayed unmade for six rounds.

### 3.1 `docker-compose.yml` — Add Alertmanager

```yaml
  alertmanager:
    image: prom/alertmanager:latest
    ports:
      - "9093:9093"
    volumes:
      - ./monitoring/alertmanager.yml:/etc/alertmanager/alertmanager.yml
    command:
      - "--config.file=/etc/alertmanager/alertmanager.yml"
```

### 3.2 `monitoring/prometheus.yml` — Point Prometheus at Alertmanager

```yaml
alerting:
  alertmanagers:
    - static_configs:
        - targets: ["alertmanager:9093"]

rule_files:
  - "/etc/prometheus/alerts/prometheus_rules.yml"   # the file with ProxyPoolCriticallyLow, already exists
```

### 3.3 `monitoring/alertmanager.yml` — Route to Slack (default) or PagerDuty (swap)

```yaml
route:
  receiver: default
  group_by: ["alertname"]
  group_wait: 30s
  group_interval: 5m
  repeat_interval: 4h
  routes:
    - match:
        severity: critical
      receiver: paging-channel
      repeat_interval: 30m   # critical alerts nag more often than default

receivers:
  - name: default
    slack_configs:
      - api_url: "${SLACK_WEBHOOK_URL}"
        channel: "#alerts"
        title: "{{ .CommonAnnotations.summary }}"
        text: "{{ .CommonAnnotations.description }}"

  - name: paging-channel
    slack_configs:
      - api_url: "${SLACK_WEBHOOK_URL}"
        channel: "#alerts-critical"
        title: "🚨 {{ .CommonAnnotations.summary }}"
        text: "{{ .CommonAnnotations.description }}"
    # --- PagerDuty alternative, use instead of/alongside slack_configs above ---
    # pagerduty_configs:
    #   - service_key: "${PAGERDUTY_SERVICE_KEY}"
    #     description: "{{ .CommonAnnotations.summary }}"
```
`SLACK_WEBHOOK_URL` goes in `.env`, never committed — same handling as
`CHALLENGE_MIRROR_SECRET_KEY` and the Postgres credentials already established in
this project.

### 3.4 Confirm The Rule Itself Still Matches The Wired Gauge

Re-paste `ProxyPoolCriticallyLow` here as a checkpoint — it was defined against
`proxy_pool_validated_count` in the round-6 critical-fixes report; confirm it's
still exactly this after any later changes:
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

### 3.5 Required Evidence — Must Be An Actual Received Page, Not A Config Read-Through

Config that "looks right" is exactly the category of claim this project has
learned not to accept at face value. Prove it fires and arrives:

```bash
# Force the condition:
docker exec -it <postgres-container> psql -U scraper -d scraper_engine \
  -c "UPDATE proxy_pool SET reliability_score = 0 WHERE reliability_score >= 40;"
# Wait for next harvest cycle (or manually call harvester._count_validated + .set())
# to push the gauge below 5, then wait out the `for: 5m` window.
```
**Required in the report:** a screenshot or raw JSON of the actual Slack message
(or PagerDuty incident) that arrived, with a timestamp, plus the Prometheus
`/api/v1/alerts` endpoint output showing the alert in `firing` state at the same
time. Then restore the test data and confirm the alert clears (`resolved` state,
and ideally a "resolved" notification if the receiver is configured to send one —
Slack webhooks do this by default; confirm it arrived too, since a page that never
tells you when it's over is its own operational hazard).

---

## Consolidated Round 7 Evidence Checklist

- [ ] Confirmed which storage_state path (A/B) applies to the real `camoufox` API
- [ ] `browser_sessions` migration applied, confirmed via `\d browser_sessions`
- [ ] `test_domain_a_then_domain_b_does_not_carry_cookies` — raw pytest pass
- [ ] `test_session_persistence` (live) — raw pytest pass + DB row evidence
- [ ] `proxy_pool` migration (`promotion_attempts`, `last_promotion_attempt_at`) applied
- [ ] One manual `promotion.run_once()` cycle, before/after `GROUP BY reliability_score`
- [ ] One proxy manually proven to promote 25→60 against a controlled judge (not relying on wild internet luck)
- [ ] Alertmanager + Prometheus wired, `docker compose up` evidence
- [ ] Actual Slack/PagerDuty message received, screenshot or raw payload, with matching `firing` state from `/api/v1/alerts`
- [ ] Alert clears and (if supported) resolution notification confirmed

No item on this list closes on "the code is in place." Every one closes on the
specific evidence next to it.
