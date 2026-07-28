# Round 8 Closure Directive — One Item Outstanding. Mandatory.

**Status: NOT CLOSED.** The last report submitted is truncated mid-sentence at
"Headline Finding — Unwired `api/rou" and contains zero new content addressing the
quota bug flagged in the prior review. Items A, B, and E in that report are
unchanged re-pastes of already-accepted work — they are not in question. This
directive covers exactly one remaining requirement. Submit nothing else until this
is done.

---

## The Bug, Restated With Zero Ambiguity

**File:** `api/routes.py`, inside `POST /v1/scrape`.

**Current code (confirmed present in the last full report):**
```python
    # 3. QuotaManager.check_and_increment()
    if _storage_redis is not None and _storage_pg is not None:
        try: await QuotaManager(redis=_storage_redis, pg=_storage_pg).check_and_increment(tenant_id, count=len(request.urls))
        except Exception: pass
```

**This is broken in two independent, compounding ways:**
1. `check_and_increment` returns a `bool`. This code discards the return value.
   Whether the tenant is under or over quota, execution proceeds to the DB insert
   regardless. **No request has ever been rejected for exceeding quota.**
2. If `core/quota.py`'s real method signature does not accept a `count=` keyword
   argument, this line raises `TypeError` on every single call — and the bare
   `except Exception: pass` swallows that too, silently. You do not currently know
   which of these two failure modes is occurring, or whether it's both. Find out.

---

## Required Actions — All Four, In Order

### 1. Paste the real, current signature of `check_and_increment`

```bash
grep -A 15 "def check_and_increment" core/quota.py
```
Paste the raw output. Do not paraphrase it. If it does not accept `count`, that
confirms failure mode 2 above and the fix below must not pass `count=`.

### 2. Fix `api/routes.py` — exact replacement, no bare exception swallow

```python
    # 3. QuotaManager.check_and_increment() — MUST be enforced, not just called
    if _storage_redis is not None and _storage_pg is not None:
        quota_ok = await QuotaManager(redis=_storage_redis, pg=_storage_pg).check_and_increment(tenant_id)
        if not quota_ok:
            raise HTTPException(status_code=429, detail="Daily quota exceeded")
```
- No `try/except` around this call. If `QuotaManager` raises, that must surface as
  a 500 during this development phase, not be masked as a silent success. A masked
  quota-check failure is worse than a visible crash — it means every request
  "succeeds" while providing zero enforcement, exactly the bug being fixed right
  now.
- Do not pass `count=` unless step 1 confirms the real method accepts it. If it
  only increments by 1 per call and you need per-URL accounting, that is a
  `core/quota.py` change to make explicitly and separately — do not silently
  under- or over-count by guessing at a parameter that may not exist.

### 3. Required evidence — must show both outcomes, not just the passing one

Seed a tenant at or near its quota ceiling and demonstrate both the accept and the
reject path in the same run:

```bash
# Set a tenant's quota low enough to exhaust in a handful of requests
docker exec scraper_engine-postgres-1 psql -U scraper -d scraper_engine \
  -c "UPDATE tenants SET quota_daily_limit = 2 WHERE tenant_id = 'system';"
# (or the Redis-side equivalent if quota state lives there — use whatever
# core/quota.py actually reads from, per step 1's answer)

# Request 1 — must succeed
curl -s -o /dev/null -w "%{http_code}\n" -X POST http://localhost:8000/v1/scrape \
  -H "X-API-Key: sk-admin" -H "Content-Type: application/json" \
  -d '{"urls":["http://example.com"]}'

# Request 2 — must succeed
curl -s -o /dev/null -w "%{http_code}\n" -X POST http://localhost:8000/v1/scrape \
  -H "X-API-Key: sk-admin" -H "Content-Type: application/json" \
  -d '{"urls":["http://example.com"]}'

# Request 3 — must be REJECTED
curl -s -X POST http://localhost:8000/v1/scrape \
  -H "X-API-Key: sk-admin" -H "Content-Type: application/json" \
  -d '{"urls":["http://example.com"]}'
```
Paste the raw output of all three curls. The third **must** show HTTP 429 with the
`"Daily quota exceeded"` body. If it doesn't, the fix is not done — resubmitting
code that "looks correct" without this exact three-request transcript will not be
accepted, per the standing rule already established this project: a code read-
through is not evidence, an observed HTTP response is.

### 4. Restore the tenant's quota to its original value after the test

```bash
docker exec scraper_engine-postgres-1 psql -U scraper -d scraper_engine \
  -c "UPDATE tenants SET quota_daily_limit = 1000 WHERE tenant_id = 'system';"
```
State the restored value in the report. Do not leave the shared dev tenant
permanently quota-limited to 2 requests/day as a side effect of producing this
evidence — that's the same class of self-inflicted footgun as Item E's shared-DB
`DELETE FROM proxy_pool` risk already on record from last round.

---

## Non-Negotiable

- Submit the **complete** report this time, not truncated. Confirm the final
  section renders fully before sending.
- This is the only open item. Do not re-paste Items A, B, or E — they are already
  accepted and closed. A report that re-submits accepted work padded around one
  still-missing fix will be read as an attempt to bury the gap in volume, not as
  thoroughness.
- If, after step 1, it turns out `check_and_increment`'s real behavior differs
  materially from what's assumed above (e.g., it's Redis-only with no
  `tenants.quota_daily_limit` column involved at all), say so explicitly and adapt
  the evidence-capture command to whatever the real storage mechanism is — do not
  force the fix to match this directive's assumed schema if the real code disagrees
  with it. Report the actual mechanism, then prove it enforces correctly on its own
  terms.
