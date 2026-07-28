# Changelog

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Entries below summarize each merged PR; full round-by-round detail (every
bug found, every design decision and why) lives in `.claude/MEMORY.md` and
`.claude/knowledge/`, not here.

## [Unreleased]

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
