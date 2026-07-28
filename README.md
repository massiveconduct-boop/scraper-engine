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
tenant creation, scaling, monitoring): `docs/guides/deployment.md`.

## Tests

```bash
docker compose up -d postgres redis pgbouncer   # integration/chaos tests need real infra
pytest tests/unit/ tests/integration/ tests/chaos/ -q
ruff check . --exclude 'tests/fixtures/challenge_mirror'
mypy src/scraper_engine/core/ src/scraper_engine/proxy/ src/scraper_engine/orchestrator/ src/scraper_engine/api/ src/scraper_engine/storage/ src/scraper_engine/fetcher/ src/scraper_engine/browser/ src/scraper_engine/observability/ --strict --ignore-missing-imports
```

## Repository Layout

| Path | What |
|---|---|
| `core/`, `proxy/`, `browser/`, `fetcher/`, `orchestrator/`, `api/`, `storage/`, `config/`, `cli/`, `observability/`, `services/` | Application source — see `CLAUDE.md` → Module Map for responsibilities |
| `tests/` | Unit, integration, chaos, and live test suites |
| `tests/fixtures/challenge_mirror/` | Self-hosted Cloudflare-like test target used for live L2/L3 anti-detection verification |
| `tests/fixtures/judge_server.py` | Self-hosted proxy judge used by the promotion integration test |
| `docs/reference/` | API reference |
| `docs/guides/` | Deployment and operational guides |
| `.claude/knowledge/` | Living architecture, decisions, standards, troubleshooting, and operations docs |
| `.claude/MEMORY.md` | Full round-by-round project history and technical debt log |
| `infra/`, `monitoring/` | PgBouncer config, Prometheus/Grafana dashboards and alert rules |
| `CHANGELOG.md` | Per-PR summary of what shipped |
| `CONTRIBUTING.md` | Dev setup, test/lint commands, PR workflow |
| `LICENSE`, `NOTICE` | Apache License 2.0 |

The authoritative design spec and a couple of build-time-only files
(superseded duplicates, one-off manual scripts) are kept locally under
`.local/`, gitignored — not part of the tracked repo. Historical per-round
evidence/directive/closure reports are similarly kept locally under
`.archive/`.

## Navigation

- **Start here for architecture/conventions:** `CLAUDE.md`
- **Contributing:** `CONTRIBUTING.md`
- **Knowledge catalog:** `.claude/MEMORY.md`
