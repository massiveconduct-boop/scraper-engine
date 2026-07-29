# Changelog

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Entries below summarize each merged PR; full round-by-round detail (every
bug found, every design decision and why) lives in `.claude/MEMORY.md` and
`.claude/knowledge/`, not here.

## [Unreleased]

- **Round 28 — chore: coverage gate wired for real + 7 senior-dev-review
  findings closed.** `pyproject.toml`'s `[tool.coverage.report] fail_under`
  was declared but no CI `pytest` invocation ever passed `--cov` — the gate
  never actually ran (real measured coverage was 72%, not the declared
  90%). Now wired into the `chaos` CI job (`--cov-fail-under=100`, run last
  once full docker-compose infra is up) and coverage brought to 100% across
  every package in `[tool.coverage.run] source` except `browser/` (real
  Firefox needed, not available in CI). Also: version bumped `0.1.0` →
  `1.0.0`; CI test matrix now covers Python 3.11 and 3.12 (previously
  3.12-only despite `requires-python = ">=3.11"`); `requirements-lock.txt` /
  `requirements-dev-lock.txt` added (`uv pip compile`) and CI/Dockerfile's
  three hand-duplicated dependency lists replaced with `pip install -r`
  against the lockfile, with a CI drift check; `pip-audit` step added to
  the lint job; Dependabot configured for `pip`/`github-actions`/`docker`;
  `py.typed` marker added (PEP 561); `SECURITY.md`, `CODEOWNERS`, issue/PR
  templates added; `.pre-commit-config.yaml` added (ruff + scoped
  `mypy --strict`, matching CI exactly) and verified via
  `pre-commit run --all-files`. Found and fixed two real bugs along the
  way: `pyproject.toml`'s `dependencies = [...]` list was textually
  misplaced after `[tool.setuptools.package-data]`, so TOML parsed it as
  nested under that table — `pip install -e .` installed zero runtime
  dependencies, silently masked because CI/Dockerfile hand-listed every
  dependency separately; and `fetcher/scrapling_wrapper.py` called a
  nonexistent `scrapling.get()` (dead code, zero callers, never covered by
  a test) — fixed to the real `scrapling.fetchers.AsyncFetcher.get()` API.
- **PR #14 — refactor: consolidate 12 top-level packages under
  `src/scraper_engine/`.** 455 import statements rewritten to the new
  `scraper_engine.<package>` namespace; a stale `types-redis` stub had been
  masking real `redis-py` types, found and removed as a follow-up fix.
- **PR #13 — fix: import-path independence.** `alembic.ini`'s
  `script_location` made cwd-independent (`%(here)s` token) — the
  documented production migration command was silently broken when run
  from inside a container.
- **PR #12 — chore: relocate test fixtures, gitignore build-time-only
  files.** `challenge-mirror/` and `judge_server.py` (real, actively-used
  test infrastructure) moved under `tests/fixtures/`; design spec and
  unused scripts moved to a gitignored `.local/`.
- **PR #10 — chore: repo layout cleanup.** Archived 63 historical per-round
  evidence/directive/closure reports out of `docs/` (categorized, gitignored
  locally rather than tracked); restructured `docs/` into `reference/` and
  `guides/`; replaced a top-level `README.md` that had been accidentally
  duplicating `challenge-mirror/README.md`; added `LICENSE` (Apache 2.0),
  `NOTICE`, `CONTRIBUTING.md`.
- **PR #9 — fix: alembic migrations inside containers.** `alembic.ini`'s
  `sqlalchemy.url` was hardcoded to `localhost:5432`, so the documented
  production migration step (`docker compose exec api alembic upgrade
  head`) failed inside the `api` container — CI never caught it because it
  runs `alembic` bare-metal on the runner, not inside a built image.
  `migrations/env.py` now prefers `DATABASE_URL` from the environment.
- **PR #8 — feat: Botasaurus capability upgrade.** `google_get(bypass_cloudflare=True)`
  for free Cloudflare-tier bypass, `tiny_profile`/anti-detection settings,
  a same-domain driver-reuse pool (`browser/botasaurus_pool.py`), and a
  JA3-TLS-fingerprint-matched client for L1 (`services/botasaurus_requests_client.py`,
  off by default). Found and fixed a real bug along the way: botasaurus's
  `@browser` decorator calls the wrapped function positionally, which
  silently clobbered a keyword-default URL parameter.
- **PR #7 — feat: observability wiring + real tracing.** Structured JSON
  logging and distributed tracing existed but were never invoked anywhere;
  wired via `observability/bootstrap.py`. Real Jaeger tracing deployed
  (not just configured) with process-wide httpx/asyncpg/redis
  instrumentation and one root span per job/harvester cycle.
- **PR #6 — feat: execution pipeline production readiness.** `POST
  /v1/scrape` / `POST /v1/crawl` now enqueue onto a real `rq` queue end to
  end, persisting results and S3 snapshots and firing webhooks. Closed a
  DNS-rebind SSRF TOCTOU gap by re-validating at fetch time and every
  redirect hop, not just at submit time.
- **PR #5 — feat: CAPTCHA provider visibility.** Provider-key health
  (NoCaptchaAI/CapSolver) made observable via metrics.
- **PR #4 — feat: proxy harvester daemon.** The daemon now actually runs on
  a schedule (was exiting immediately after each deploy).
- **PR #3 — fix: centralize connection config.** Single source of truth for
  DB/Redis/S3 connection settings, removing scattered duplicate config.
- **PR #2 — docs: round 20 shipped.**
- **PR #1 — feat: rounds 12-17.** Bulk of early blueprint implementation
  brought onto the PR-reviewed workflow.

## [1.0.0-rc1] - 2026-07-26

Full blueprint implementation through round 12: all 7 design invariants
enforced, CAPTCHA solving (NoCaptchaAI primary / CapSolver fallback) wired
into L2/L3, branch protection enabled on `main`.
