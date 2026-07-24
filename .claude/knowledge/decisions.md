# Design Decisions

**Purpose:** Record WHY decisions were made, TRADEOFFS considered, ALTERNATIVES rejected.
**Scope:** Irreversible or high-cost decisions. Routine implementation choices excluded.
**When to read:** Before changing architecture; when a decision seems wrong and needs context.
**Related:** `specs/scraper-engine-blueprint-v2.md`, `.claude/knowledge/architecture.md`

---

## Decision: proxybroker2 Subprocess Isolation

**Date:** 2026-07-24 | **Round:** 4-5

**What:** proxybroker2 runs in isolated `asyncio.create_subprocess_exec()` with venv Python, returning JSON via stdout. Not imported in-process.

**Why:** proxybroker2 uses aiohttp; harvester imports httpx. Combined imports caused source flakiness (with vs without httpx produced different proxy counts in early tests). Later disproved as the root cause (both return 3 proxies in same-script test), but subprocess isolation remains as defense-in-depth.

**Tradeoffs:** Adds ~0.5s subprocess overhead per harvest cycle. Guarantees no aiohttp/httpx event loop conflict regardless of diagnosis.

**Alternatives:** In-process import of proxybroker2 (rejected — unpredictable event loop behavior). Direct scraping only (rejected — proxybroker2 provides judge-validated proxies).

**Status:** Active. Subprocess isolation is defense-in-depth, not root cause fix.

---

## Decision: Two-Tier Proxy Scoring

**Date:** 2026-07-24 | **Round:** 6

**What:** TCP-probe-only proxies scored at 25 (below L1 threshold 40). HTTP-validated proxies scored at 60.

**Why:** Free proxies have ~0.02% HTTP forwarding success rate. TCP probe catches 96% of dead ones cheaply (connection refused = instant reject). Full HTTP validation catches the remaining 4% but takes 5s each. Two-tier ensures pool is never ~99.6% dead weight while still identifying the rare working HTTP proxies.

**Tradeoffs:** TCP-only proxies are quarantined (cannot be selected by ProxyManager). `promote_tcp_only()` background job re-validates them. Until promoted, pool relies on broker-validated proxies (score 60) or occasional HTTP-validated direct-scrape proxies.

**Alternatives:** Validate everything through HTTP (rejected — impossible to get 40+ proxies in reasonable time). Validate nothing, accept dead pool (rejected — blueprint §2).

**Status:** Active.

---

## Decision: `lease()` Async Context Manager

**Date:** 2026-07-24 | **Round:** 6

**What:** `pool.lease(proxy, domain)` wraps acquire/release in try/finally. Callers use `async with pool.lease() as ctx:`.

**Why:** Hot-browser pool rewrite (`acquire()` returning bare context) broke invariant §1.1.6 — context cleanup was no longer guaranteed on exception. `lease()` restores the structural guarantee: release ALWAYS runs, healthy on normal exit, unhealthy (teardown) on exception.

**Tradeoffs:** Adds one level of indirection. acquire() and release() remain public (should be prefixed `_acquire`/`_release` — deferred).

**Alternatives:** Restore CamoufoxWrapper as return type (rejected — wrapper's __aexit__ always tears down, can't support healthy re-queue).

**Status:** Active. Lease is the contract. acquire/release are internal.

---

## Decision: PgBouncer Auto-Entrypoint

**Date:** 2026-07-24 | **Round:** 5-6

**What:** `pgbouncer-init` Docker service queries Postgres for SCRAM verifier, writes userlist.txt to shared volume. PgBouncer mounts shared volume. Zero manual steps.

**Why:** edoburu/pgbouncer Docker image auto-generates MD5 userlist. Postgres 16 requires SCRAM-SHA-256. MD5→SCRAM mismatch causes "wrong password type" on every connect. Dynamic SCRAM regeneration from `pg_authid.rolpassword` solves authentication permanently.

**Tradeoffs:** Adds one init container. Requires pg_hba.conf rule `host all all 172.0.0.0/8 md5` for PgBouncer→Postgres forwarding on Docker bridge network.

**Alternatives:** Static userlist.txt file (rejected — breaks when Postgres container is recreated with new SCRAM salt). auth_query (rejected — chicken-and-egg: PgBouncer needs auth to query for auth).

**Status:** Active.

---

## Decision: `acquire()` Classify-Once Pattern

**Date:** 2026-07-24 | **Round:** 6

**What:** Every item drained from pool is classified exactly once: selected, kept, or torn down. Only `keep` goes back into `self._pool`.

**Why:** Prior implementation re-queued ALL items to pool before selecting one. Selected item stayed in queue — second acquire() handed same live context to two callers. Classify-once prevents double-issue structurally, not via caller discipline.

**Tradeoffs:** More complex than simple queue.get_nowait(). But prevents a class of concurrency bugs that caller discipline cannot reliably prevent.

**Alternatives:** Remove and re-queue separately (rejected — the bug this decision fixes). Lock around acquire/release (rejected — overkill for async Python; pool is already single-threaded via asyncio).

**Status:** Active. Regression tests (`TestAcquireDoubleIssue`) catch reoccurrence.

---

## Decision: Free Proxy Sources Only

**Date:** 2026-07-24 | **Round:** 6

**What:** 6 operators accepted as permanent ceiling. Blueprint's "50+ sources" language retired.

**Why:** 50+ independently-operated free proxy sources do not meaningfully exist. Chasing the number had diminishing returns. 5-6 sources across 5 failure domains is the real-world ceiling for free-tier sourcing.

**Tradeoffs:** Fewer sources = less resilience to any single source going dark. Mitigated by `ProxyPoolCriticallyLow` alert firing on validated count <5.

**Alternatives:** Paid proxy services (rejected by product owner — "free only" constraint). Building proprietary proxy scraper with 50+ websites (rejected — maintenance burden exceeds benefit).

**Status:** Active. Product owner decision.

---

## Decision: PostgreSQL 16 with SCRAM-SHA-256

**Date:** 2026-07-24 | **Round:** 5

**What:** PostgreSQL 16 enforces SCRAM-SHA-256 for host connections. PgBouncer must use matching auth_type.

**Why:** Postgres 16 changed default password encryption from md5 to scram-sha-256. The edoburu/pgbouncer image only generates md5 hashes. Dynamic SCRAM regeneration solves the mismatch.

**Status:** Active. `infra/pgbouncer/userlist.txt` auto-regenerated by pgbouncer-init.
