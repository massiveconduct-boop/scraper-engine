# Scraper Engine — Deployment Guide

## Prerequisites

- Docker 24+ with Compose v2
- Python 3.11+ (for local dev)
- PostgreSQL 16, Redis 7, MinIO (included via docker-compose)

## Quick Start (Development)

```bash
# Clone and install
git clone <repo-url> scraper_engine
cd scraper_engine
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# Copy env
cp .env.example .env
# Edit .env: add CapSolver key, Firecrawl key if using those services

# Start infrastructure
docker compose up -d postgres redis

# Run migrations
alembic upgrade head

# Start API server
uvicorn api.main:app --host 0.0.0.0 --port 8000 --reload

# Verify
curl http://localhost:8000/v1/health
```

## Production Deployment

### 1. Environment Setup

```bash
# Required env vars
export PGBOUNCER_DSN="postgresql://scraper:<password>@pgbouncer:6432/scraper_engine"
export REDIS_URL="redis://redis:6379/0"
export S3_ENDPOINT="http://minio:9000"
export S3_ACCESS_KEY="<access-key>"
export S3_SECRET_KEY="<secret-key>"
export CAPSOLVER_API_KEY="<capsolver-key>"
export APP_ENV="production"
export LOG_LEVEL="WARNING"
```

### 2. Start Full Stack

```bash
docker compose up -d
```

Services started:
- `api` — FastAPI on port 8000
- `worker-l1`, `worker-l2`, `worker-l3` — RQ workers per escalation level
- `proxy-harvester` — background proxy discovery
- `postgres` — primary database
- `pgbouncer` — connection pooler (transaction mode, max 500 clients)
- `redis` — queue + cache
- `minio` — S3-compatible snapshot storage

### 3. Run Migrations

```bash
docker compose exec api alembic upgrade head
```

### 4. Create Admin Tenant

```bash
docker compose exec api python cli/entrypoint.py create-tenant <name>
# Saves API key — store securely
```

### 5. Health Check

```bash
curl http://localhost:8000/v1/health
# Expected: {"status":"ok","proxy_pool_size":...,"pgbouncer_reachable":true,...}
```

## Infrastructure Configuration

### PgBouncer (BD-06)

- Pool mode: `transaction`
- Max client connections: 500
- Default pool size: 20
- File: `infra/pgbouncer/pgbouncer.ini`

### S3 Retention (BD-07)

- Successful snapshots: 1 day auto-delete
- Failed snapshots: 30 day retention
- Applied as lifecycle policy on bucket creation

### CapSolver Budget (BD-03)

- Default: $1.00/day per tenant hard ceiling
- Enforced atomically in Redis via Lua script
- Change in: `config/base.yaml` → `capsolver.daily_credit_ceiling_default`

## Scaling

| Dimension | Default | Scaling strategy |
|---|---|---|
| API | 1 replica | Add more `api` replicas behind load balancer |
| Workers | 1 per level | Scale `worker-l1` first (most traffic), add replicas |
| Postgres | Single | Add read replicas, consider Citus for multi-tenant |
| Redis | Single | Sentinel for HA, cluster for sharding |
| Browser pool | 8 instances | Increase `camoufox.max_total_instances`, add more worker replicas |

## Monitoring

- Prometheus metrics: `:9090/metrics`
- Grafana dashboard: `monitoring/dashboards/grafana_overview.json`
- Alert rules: `monitoring/alerts/prometheus_rules.yml`

Key alerts:
- `ProxyPoolCriticallyLow` — elite proxies < 5
- `DeadLetterQueueGrowing` — DLQ > 100 entries
- `CapSolverBudgetExhausted` — spend > 90% of $1.00/day ceiling
- `HighJobFailureRate` — > 50% failure rate in 10 min window

## Security

- API keys: `sk-` prefix, 40 chars alphanumeric, stored in `public.api_keys`
- SSRF guard: blocks all private/loopback/link-local IPs before enqueue
- SQL injection: `TenantId` regex validation before any DDL construction
- Middleware: rate limiting (100 req/min), 1 MB body limit, security headers
- Never expose metrics endpoint (`/v1/metrics`) to public internet

## Troubleshooting

| Symptom | Check |
|---|---|
| `403` on scrape | API key valid? Tenant exists? |
| `429` on scrape | Rate limited — wait 60s |
| `413` on scrape | Body > 1 MB — reduce batch size |
| Jobs stuck `PENDING` | Worker running? `docker compose ps worker-*` |
| All jobs fail L3 | Proxy pool empty? Check `proxy_pool_size` metric |
| PgBouncer unreachable | `docker compose logs pgbouncer` |
| Migration fails | Run `alembic current` to check state |
