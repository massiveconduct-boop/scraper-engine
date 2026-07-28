# Contributing

## Dev setup

```bash
git clone <repo-url> scraper_engine
cd scraper_engine
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

cp .env.example .env
docker compose up -d postgres redis pgbouncer
alembic upgrade head
```

## Before opening a PR

```bash
pytest tests/unit/ tests/integration/ tests/chaos/ -q
ruff check . --exclude 'tests/fixtures/challenge_mirror'
mypy core/ proxy/ orchestrator/ api/ storage/ fetcher/ browser/ observability/ --strict --ignore-missing-imports
```

Integration and chaos tests need real Postgres/Redis/PgBouncer — start them
first with the `docker compose up -d` command above. mypy's baseline
(`tools/mypy-baseline.txt`) is empty; any new error fails CI, don't add
suppressions to work around it — fix the type issue.

## Workflow

- One topic per PR. Branch off `main`, e.g. `fix/...` or `feat/...`.
- Push and open a PR — CI runs lint, unit, integration, and chaos jobs
  automatically (`.github/workflows/test.yml`). All four must pass before
  merge; `build-and-push` only runs on merge to `main` and publishes the
  image to GHCR.
- Prefer a regular merge over squash/rebase so the PR's own commit history
  stays intact on `main`.
- Root-cause fixes over patches: if something's broken, fix the underlying
  issue rather than working around it. Back non-obvious claims with real
  terminal output or a source reference, not paraphrased numbers.

## Project context

- `CLAUDE.md` — architecture, module map, operating conventions
- `.claude/knowledge/` — architecture, decisions, standards, troubleshooting,
  operations (living documents, kept current)
- `.claude/MEMORY.md` — full round-by-round project history and technical
  debt log

The design spec (7 non-negotiable design invariants and the full module
blueprint) isn't part of this repo — ask a maintainer if you need it.
