# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Status

**Blueprint phase** — no source code written yet. single source of truth is `specs/scraper-engine-blueprint-v2.md` (1000-line implementation-grade spec). Code generation should follow its module structure, interfaces, and design invariants exactly.

## Architecture Overview

Search & Scraper Engine — async Python system for multi-level web scraping with anti-detection, proxy management, and SSRF safety.

### Stack
- **Runtime:** Python 3.11+ asyncio
- **API:** FastAPI
- **Browser automation:** Camoufox (anti-detect Playwright fork) + Botasaurus
- **HTTP:** httpx + Scrapling
- **Queue:** RQ (Redis)
- **Storage:** PostgreSQL (PgBouncer-fronted), Redis, S3/MinIO
- **Config:** YAML + Pydantic
- **Observability:** Prometheus metrics
- **Testing:** pytest (unit + integration + chaos)

### Module Layout (from spec)
```
core/          — Domain models, TenantId, SSRF guard, retry strategies, budgets, quotas
proxy/         — Harvester (proxy discovery), Manager (selection), Scoring, Leases, Health monitoring
browser/       — Camoufox wrapper, pre-warmed pool, session state persistence
fetcher/       — 3-level fetch (L1: HTTP, L2: Botasaurus+Camoufox, L3: Camoufox-only), challenge detection
orchestrator/  — Worker, politeness controller, circuit breaker, webhook
api/           — FastAPI routes, auth (API key → TenantId), health endpoint
storage/       — Postgres, Redis, S3 clients; dedup cache; fingerprint store; DLQ
config/        — Pydantic schema, YAML loader, env-specific files
cli/           — CLI entrypoint
observability/ — Prometheus metrics, structured logging, alert rules
```

### Design Invariants (non-negotiable, from spec §1.1)
1. No component calls proxybroker2 HTTP control API — all proxy state lives in our Postgres/Redis.
2. Camoufox owns 100% of fingerprint/UA/canvas/WebGL spoofing. App code never touches those.
3. `tenant_id` is explicit validated `TenantId` value object everywhere — no ambient ContextVar at trust boundaries.
4. Every outbound fetch is SSRF-checked before enqueue and after every redirect.
5. Nothing cached as success unless `FetchResult.success is True` and not challenge page.
6. Every resource acquisition has guaranteed release path (context manager or TTL, never both for same resource).
7. SQL identifiers validated against allow-list regex before interpolation.

### Escalation State Machine (core workflow)
```
PENDING → CIRCUIT_CHECK → FETCHING_L1 → PARSING_L1
                                      ↘ failure → ESCALATING_L2 → FETCHING_L2 → PARSING_L2
                                                                               ↘ failure → ESCALATING_L3 → ...
                                                                                                            ↘ DEAD_LETTER
```

## Key Decisions (from spec)

| Decision | Choice | Rationale |
|---|---|---|
| DB access | Single asyncpg pool → PgBouncer → per-tenant `SET search_path` | Avoids N pools × M tenants connection multiplication |
| Browser pool | Semaphore-gated; no unbounded fallback path | Prevents process leak under load |
| Botasaurus | Always `parallel=1` when called from our orchestrator | Nested concurrency control would multiply processes |
| Circuit breaker | 3-state (closed/open/half-open) with exponential backoff across trips | Prevents thundering herd on recovery |
| Proxy selection | Bounded loop, no recursion | Eliminates RecursionError on pool exhaustion |
| Dedup | Only caches if `FetchResult.success=True` and not challenge page | Prevents caching blocked/detection pages |

## Resolved Blocking Dependencies (BD-01 through BD-07)

All 7 resolved 2026-07-21. See spec §0 for full decision table. Summary:
- **BD-01**: Proxy sources — verify proxifly/proxyscrape/iplocate/proxripper before impl; find alternatives if dead
- **BD-02**: Camoufox baked into Docker image at build time
- **BD-03**: CapSolver ceiling **$1.00/day** (default in schema changed from 5.0 → 1.0)
- **BD-04**: Tenant provisioning built into system — admin endpoints create tenants + API keys
- **BD-05**: `tests/live/` uses self-hosted Cloudflare-challenge-page mirror
- **BD-06**: PgBouncer `max_client_conn=500` `default_pool_size=20`
- **BD-07**: S3 retention: failed snapshots 30 days, successful snapshots 1 day, auto-deletion scheduled

## Development Commands

```bash
# Install dependencies (when code exists)
pip install -r requirements.txt
pip install -r requirements-dev.txt

# Run tests
pytest                          # all tests
pytest tests/unit/              # unit tests only
pytest tests/integration/       # integration tests only
pytest tests/chaos/             # chaos tests (slow, not per-PR)
pytest -k "test_ssrf"          # single test pattern
pytest --cov=core --cov=proxy --cov=orchestrator --cov-report=term-missing  # coverage

# Lint / type-check
ruff check .
mypy core/ proxy/ orchestrator/

# Run locally
uvicorn api.main:app --reload

# RQ worker
rq worker scraper-{level1,level2,level3}
```

## OpenWolf Rules

This project uses OpenWolf for context management:
- Read `.wolf/STATUS.md` FIRST at session start
- Check `.wolf/anatomy.md` before reading any file
- Check `.wolf/cerebrum.md` before generating code (Do-Not-Repeat section)
- Update `.wolf/STATUS.md` when quest completes or before /clear
- Append to `.wolf/memory.md` after significant actions
- Log bugs to `.wolf/buglog.json` (low threshold)

