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

pre-commit install
```

`pre-commit install` is a one-time step per clone — after that, every commit
runs `ruff check --fix`, `ruff format`, and the same scoped `mypy --strict`
CI runs, so lint/format/type issues are caught before they reach a PR.

`pip install -e ".[dev]"` registers every top-level package (`api`, `core`,
`proxy`, etc.) via an editable-install finder, so imports resolve to their
real path regardless of the process's current working directory — you don't
need `PYTHONPATH` tricks or to run commands from the repo root specifically.

**Import style:** absolute cross-package imports (`from core.tenant import
TenantId`), single-dot same-package imports are fine (`from .schema import
AppConfig` inside `config/`), no deep relative imports (`from ..x` /
`from ...x`) — none exist in this codebase, keep it that way.

## Before opening a PR

```bash
pytest tests/unit/ tests/integration/ tests/chaos/ --cov=src/scraper_engine --cov-report=term-missing --cov-fail-under=100
ruff check . --exclude 'tests/fixtures/challenge_mirror'
mypy src/scraper_engine/core/ src/scraper_engine/proxy/ src/scraper_engine/orchestrator/ src/scraper_engine/api/ src/scraper_engine/storage/ src/scraper_engine/fetcher/ src/scraper_engine/browser/ src/scraper_engine/observability/ --strict --ignore-missing-imports
```

Integration and chaos tests need real Postgres/Redis/PgBouncer — start them
first with the `docker compose up -d` command above. mypy's baseline
(`tools/mypy-baseline.txt`) is empty; any new error fails CI, don't add
suppressions to work around it — fix the type issue. Coverage is gated at
100% (`[tool.coverage.report] fail_under` in `pyproject.toml`) across every
package except `browser/` (needs a real Firefox process, not run in CI) —
new code needs tests, not `# pragma: no cover`, unless a line is genuinely
unreachable in-process.

If you changed `pyproject.toml`'s dependencies, regenerate both lockfiles
(`uv` must be installed — `pip install uv` or see astral.sh/uv) so CI's
drift check doesn't fail. `--python-version 3.11` matches
`requires-python`'s floor — compiling without it resolves against whatever
interpreter you're running locally and can silently pick a package version
that doesn't support 3.11 (numpy==2.5.1 broke the 3.11 CI job this way):

```bash
uv pip compile pyproject.toml --python-version 3.11 --emit-index-url --no-header -o requirements-lock.txt
uv pip compile pyproject.toml --extra dev --python-version 3.11 --emit-index-url --no-header -o requirements-dev-lock.txt
```

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

## Release process

`pyproject.toml`'s `version`, the `CHANGELOG.md` `[Unreleased]` section, and
git tags must move together — a version bump with no matching tag or
changelog entry is how this repo ended up 44 commits past `v1.0.0-rc1`
with `version = "0.1.0"` still in `pyproject.toml`. To cut a release:

1. Rename `CHANGELOG.md`'s `## [Unreleased]` heading to `## [X.Y.Z] -
   YYYY-MM-DD` (today's date) and add a fresh empty `## [Unreleased]`
   above it for whatever comes next.
2. Bump `version` in `pyproject.toml` to match `X.Y.Z`.
3. Commit both together (e.g. `chore: release X.Y.Z`), merge to `main`.
4. Tag the merge commit: `git tag -a vX.Y.Z -m "vX.Y.Z" && git push origin
   vX.Y.Z`. `build-and-push` (CI) publishes the image on every merge to
   `main` regardless of tags, so the tag is purely the human-readable
   version marker — it does not itself trigger a build.

## Project context

- `CLAUDE.md` — architecture, module map, operating conventions
- `.claude/knowledge/` — architecture, decisions, standards, troubleshooting,
  operations (living documents, kept current)
- `.claude/MEMORY.md` — full round-by-round project history and technical
  debt log

The design spec (7 non-negotiable design invariants and the full module
blueprint) isn't part of this repo — ask a maintainer if you need it.
