# Scraper Engine

Async Python multi-level web scraping system with anti-detection, proxy
management, SSRF safety, and multi-tenant quota enforcement.

- **L1** — plain HTTP (Scrapling), optional JA3-TLS-fingerprint client
- **L2** — Botasaurus + Camoufox (Firefox-based anti-detect browser)
- **L3** — Camoufox-only, full anti-detection escalation

Jobs escalate L1 → L2 → L3 automatically based on failure/challenge
detection. See `CLAUDE.md` for full architecture, module map, and the
current round's changes.

## Quick Start

```bash
git clone <repo-url> scraper_engine
cd scraper_engine
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

cp .env.example .env   # add CapSolver/Firecrawl keys if using those services

docker compose up -d postgres redis pgbouncer
alembic upgrade head
uvicorn api.main:app --host 0.0.0.0 --port 8000 --reload

curl http://localhost:8000/v1/health
```

Full production deployment steps (docker-compose full stack, migrations,
tenant creation, scaling, monitoring): `docs/deployment.md`.

## Tests

```bash
docker compose up -d postgres redis pgbouncer   # integration/chaos tests need real infra
pytest tests/unit/ tests/integration/ tests/chaos/ -q
ruff check . --exclude 'challenge-mirror' --exclude 'report-review-fix'
mypy core/ proxy/ orchestrator/ api/ storage/ fetcher/ browser/ observability/ --strict --ignore-missing-imports
```

## Repository Layout

| Path | What |
|---|---|
| `core/`, `proxy/`, `browser/`, `fetcher/`, `orchestrator/`, `api/`, `storage/`, `config/`, `cli/`, `observability/`, `services/` | Application source — see `CLAUDE.md` → Module Map for responsibilities |
| `tests/` | Unit, integration, chaos, and live test suites |
| `docs/` | Current reference docs (`api-reference.md`, `deployment.md`); `docs/archive/` holds historical per-round evidence/directive reports, kept for record but not living documentation |
| `.claude/knowledge/` | Living architecture, decisions, standards, troubleshooting, and operations docs |
| `.claude/MEMORY.md` | Full round-by-round project history and technical debt log |
| `specs/` | Authoritative blueprint spec |
| `challenge-mirror/` | Self-hosted Cloudflare-like test target used for live L2/L3 anti-detection verification (not part of the production stack) |
| `infra/`, `monitoring/` | PgBouncer config, Prometheus/Grafana dashboards and alert rules |

## Navigation

- **Start here for architecture/conventions:** `CLAUDE.md`
- **Knowledge catalog:** `.claude/MEMORY.md`
- **Spec:** `specs/scraper-engine-blueprint-v2.md`
