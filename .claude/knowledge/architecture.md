# Architecture

**Purpose:** System design, invariants, module interactions, data flow.
**Scope:** Complete system architecture. Does NOT duplicate the specification — references it.
**When to read:** Understanding how components connect; adding new modules; debugging cross-cutting concerns.
**Related:** `specs/scraper-engine-blueprint-v2.md`, `.claude/knowledge/decisions.md`

---

## Design Invariants (from spec §1.1 — non-negotiable)

1. No component calls proxybroker2 HTTP control API — all proxy state in Postgres/Redis.
2. Camoufox owns 100% of fingerprint/UA/canvas/WebGL surface — app code never touches.
3. `tenant_id` is explicit `TenantId` value object everywhere — no ambient ContextVar at trust boundaries.
4. Every outbound fetch is SSRF-checked before enqueue and after every redirect.
5. Nothing cached as success unless `FetchResult.success is True` and not challenge page.
6. Every resource acquisition has guaranteed release path — context manager or TTL, never both.
7. SQL identifiers validated against allow-list regex before interpolation.

---

## Escalation State Machine

```
PENDING → CIRCUIT_CHECK → FETCHING_L1 → PARSING_L1
                                      ↘ failure → ESCALATING_L2 → FETCHING_L2 → PARSING_L2
                                                                               ↘ failure → ESCALATING_L3 → FETCHING_L3
                                                                                                              ↘ failure → DEAD_LETTER
Non-retryable (SSRF, quota, proxy exhausted): direct → DEAD_LETTER
```

Levels: L1 (httpx/Scrapling, timeout 20s, any proxy), L2 (Botasaurus+Camoufox, timeout 40s, anonymous+ proxy), L3 (Camoufox-only, timeout 60s, elite proxy).

---

## Proxy Pipeline

```
harvest_once()
  ├─ _direct_scrape()        [PRIMARY — 5-10 proxies in ~5s]
  │   ├─ 8 source URLs → _scrape_one() per source
  │   │   ├─ _parse_ip_port() / _parse_geonode()
  │   │   ├─ _tcp_probe() (2s timeout)
  │   │   └─ _http_validate() through self-hosted judge (:8089)
  │   │       └─ Score: TCP-only=25 (below L1), validated=60 (above L1)
  │   └─ Persist to proxy_pool with anonymity_level + reliability_score
  │
  └─ _harvest_via_broker()   [SUPPLEMENTARY — 1-5 validated in ~20s]
      └─ proxybroker2 subprocess (30s timeout)
          └─ broker.find() → validate → JSON stdout → persist
```

**Self-hosted judge:** `judge_server.py` on port 8089. Echoes headers + origin. Replaces httpbin.org dependency.

**Source diversity:** 8 URLs across 6 operators (proxyscrape.com, openproxylist.xyz, TheSpeedX/GitHub, monosans/GitHub, pubproxy.com, geonode.com). 5 real failure domains (GitHub CDN shared by two repos).

**Scoring:** Two-tier. TCP-only=25 (below L1 threshold 40 — cannot be selected). HTTP-validated=60. `promote_tcp_only()` background job re-validates TCP-only proxies.

---

## Browser Pool

**Design:** Hot-browser pool with real reuse. `pool.start(N)` launches N Camoufox instances and stores live contexts in an asyncio.Queue. `pool.lease(proxy, domain)` is the async context manager — returns a live context, guarantees release (structural cleanup per invariant §1.1.6).

**Key methods:**
- `start()` — launches prewarm_count browsers, stores (context, wrapper, idle_since)
- `acquire(domain)` — classifies drained items as selected/keep/teardown per idle timeout + domain matching
- `release(ctx, healthy)` — healthy returns to pool, unhealthy tears down
- `lease(proxy, domain)` — async context manager wrapping acquire/release
- `shutdown()` — tears down all live contexts

**Safety properties:**
- Semaphore-gated: `BROWSER_SEMAPHORE` prevents unbounded spawn (F-14).
- No double-issue: `acquire()` classifies each item exactly once — selected item never re-queued.
- Process cleanup: `__aexit__` always runs, browser process reaped.
- Domain guard: `lease(domain=X)` only reuses context whose `_last_domain` matches.

---

## PgBouncer

**Architecture:** `pgbouncer-init` Docker service auto-regenerates SCRAM userlist from Postgres `pg_authid.rolpassword`. PgBouncer mounts shared volume. Zero manual steps.

**Transaction pooling:** `PostgresClient.acquire()` wraps SET search_path in `BEGIN...COMMIT` to ensure all statements hit the same backend connection.

---

## Data Flow

```
API Client → FastAPI (/v1/scrape) → RQ Queue → Worker
                                                ├─ CircuitBreaker.allow_request()
                                                ├─ PolitenessController.acquire_slot()
                                                ├─ ProxyManager.get_proxy() → ProxyHarvester.harvest_once()
                                                ├─ Level1/2/3Fetcher.fetch() → FetchResult
                                                └─ DedupEngine → PostgresClient → proxy_pool
```
