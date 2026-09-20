# Operations & Deployment

**Purpose:** Infrastructure, deployment, CI, monitoring, alerts.
**Scope:** How to run, deploy, monitor, and debug this system in production.
**When to read:** Deploying; setting up CI; configuring alerts; production incidents; adding a new dependency; changing the CI job matrix.
**Keywords:** CI pipeline, GitHub Actions, branch protection, required
status checks, docker compose, PgBouncer, monitoring, Prometheus, alerts,
scaling, config-driven timeouts, known operational gaps, lockfiles,
dependency drift, webhook sweeper, DLQ reaper, proxy pool health, Slack
alerting overlap.
**Dependencies:** `.github/workflows/test.yml`, `docker-compose.yml`,
`pyproject.toml`, `requirements-lock.txt` / `requirements-dev-lock.txt` —
this document describes their live, current behavior; check those files
directly if this doc and reality ever disagree.
**Related:** `docs/guides/deployment.md`, `.claude/knowledge/architecture.md`, `.claude/knowledge/technical-debt.md`

---

## Infrastructure

| Service | Image | Port | Override var | Purpose |
|---|---|---|---|---|
| PostgreSQL | 16-alpine | 5432 | `POSTGRES_PORT` | Primary database |
| PgBouncer | edoburu/pgbouncer:latest | 6432 | `PGBOUNCER_PORT` | Connection pooler (transaction mode) |
| PgBouncer exporter | prometheuscommunity/pgbouncer-exporter | 9127 | `PGBOUNCER_EXPORTER_PORT` | Real pool-state metrics for Prometheus |
| Redis | 7-alpine | 6379 | `REDIS_PORT` | Queue + cache |
| MinIO | minio/minio:latest | 9000 (API), 9001 (console) | `MINIO_API_PORT`, `MINIO_CONSOLE_PORT` | S3-compatible storage |
| API | uvicorn | 8000 | `API_PORT` | FastAPI server |
| Workers L1/L2/L3 | RQ | — | — | Escalation-level queue workers |
| Proxy harvester | standalone Python | — | — | Background proxy collection + self-healing (round 34 — reacts to a Redis kick signal from exhausted requests, not just its own timer; also runs the per-tier pool-health cycle) |
| Webhook sweeper | standalone Python | — | — | Round 34 — drains `webhook_outbox` (retries failed/crashed deliveries with backoff; the rq work-horse that made the original attempt is too short-lived to own retry state) |
| DLQ reaper | standalone Python | — | — | Round 34 — auto-retries `dead_letter_queue` entries in the transient category (`PROXY_EXHAUSTED`, `CIRCUIT_OPEN`) once the condition that caused them clears |
| `migrate` | same image as `api` | — | — | One-shot `alembic upgrade head`, gates every Postgres-writing service via `depends_on: condition: service_completed_successfully` — see Migrations below |
| Prometheus | prom/prometheus:latest | 9090 | `PROMETHEUS_PORT` | Metrics collection + alert evaluation. Live `docker-compose.yml` service (previously config-only — `infra/prometheus/prometheus.yml` existed, git-tracked, but was never wired in) |
| Alertmanager | prom/alertmanager:latest | 9093 | `ALERTMANAGER_PORT` | Alert routing to Slack (two-tier: default + paging-channel). Live `docker-compose.yml` service — same "config existed, never wired" story as Prometheus |
| PgBouncer init | postgres:16-alpine | — | — | SCRAM userlist auto-regeneration |
| Jaeger | jaegertracing/all-in-one:latest | 16686 (UI), 4317 (OTLP gRPC), 4318 (OTLP HTTP) | `JAEGER_UI_PORT`, `JAEGER_OTLP_GRPC_PORT`, `JAEGER_OTLP_HTTP_PORT` | Distributed tracing backend — round 24 |

Every host-side port above is overridable via its env var (e.g. `API_PORT=8010 docker compose up -d`) or by setting it in `.env` — container-to-container traffic is unaffected since services address each other by service name, not host port. Ports shown are the defaults, unchanged from before this was made overridable.

---

## Migrations

`docker compose up -d` runs `alembic upgrade head` automatically via a one-shot `migrate` init service (same shape as `pgbouncer-init`) — every Postgres-writing service (`api`, `worker-l1/l2/l3`) declares `depends_on: migrate: condition: service_completed_successfully`, so nothing starts against a stale schema. A fresh `docker compose up -d` no longer requires a manual `alembic upgrade head` step. To manually re-run or check migration state (e.g. after adding a new migration file to an already-running stack): `docker compose run --rm migrate` or `docker compose exec api alembic upgrade head` (both work; `alembic upgrade head` is idempotent).

---

## Quick Start

```bash
docker compose up -d  # migrations run automatically via the `migrate` service
```

---

## Start All Services

```bash
docker compose up -d
docker compose logs -f  # watch logs
```

**Container vs host hostnames (round 20 deploy fix).** `.env` sets
`REDIS_URL=redis://localhost:6379/0` and `DATABASE_URL=...@localhost:5432...`
for **host** tools (alembic, `tools/` scripts). Inside containers `localhost`
is the container itself, so the app services (`api`, `worker-l1/l2/l3`)
each carry a compose `environment:` block overriding these to
the service hostnames — `redis://redis:6379/0` and
`...@pgbouncer:6432/scraper_engine` (DB through PgBouncer, invariant G-05).
Compose `environment:` wins over `env_file:`, so `.env` keeps localhost while
containers get service names. Symptom if missing:
`Error 111 connecting to localhost:6379. Connection refused`, workers Exited(1).

**Self-healing daemons live inside the `api` container now (Round 35).**
`proxy-harvester`, `dlq-reaper`, and `webhook-sweeper` are no longer
separate `docker-compose.yml` services/containers — they were, but nobody
was starting them (the documented Quick Start command never mentioned
them by name), so the proxy pool went stale with zero operator-visible
signal. `docker/supervisord.conf` now runs all 4 long-running processes
(`api` + the 3 daemons) as supervised subprocesses of one `scraper_engine-
api-1` container; `worker-l1/l2/l3` stay separate (different scaling
unit). Practical consequences:
- `docker compose ps` no longer shows `proxy-harvester`/`dlq-reaper`/
  `webhook-sweeper` rows — that's expected, not a regression.
- Check daemon health with `docker exec scraper_engine-api-1
  supervisorctl status` (no `-c` flag needed — the conf is also copied to
  `/etc/supervisor/supervisord.conf`, supervisorctl's default search
  path). Expect all 4 `RUNNING`.
- Each daemon crash-restarts independently (`autorestart=true`,
  `startretries=10`) without taking the others or the API down — verified
  live by `kill -9`-ing `proxy-harvester`'s PID and confirming
  `supervisorctl status` showed a new PID within ~6s while `api` kept
  serving `/v1/health` throughout.
- Old troubleshooting/decisions/operations entries that say "the
  `proxy-harvester` container" meant a literal separate container at the
  time they were written — as of round 35, read that as "the
  `proxy-harvester` process inside the `api` container." The process-
  boundary reasoning in those entries (separate OS process, separate
  in-process metrics registry, etc.) is still accurate; only the
  container topology changed.

---

## Configuration

| File | Purpose |
|---|---|
| `docker-compose.yml` | Service definitions, networks, volumes |
| `.env` | Secrets (NOCAPTCHA_AI_API_KEY primary + CAPSOLVER_API_KEY fallback, Postgres/MinIO/Slack) |
| `config/base.yaml` | Application config (timeouts, retries, quotas) |
| `pyproject.toml` | Python project config, lint rules, test settings |
| `infra/pgbouncer/pgbouncer.ini` | PgBouncer config (pool mode, max clients, auth) |
| `infra/pgbouncer/userlist.txt` | PgBouncer SCRAM userlist (auto-regenerated by pgbouncer-init) |

---

## Monitoring

Prometheus + Alertmanager are live `docker-compose.yml` services (round-N fix — the config below existed and was accurate long before that, but nothing actually ran it; `docker compose up -d` now starts both for real). `SLACK_WEBHOOK_URL` in `.env` is picked up automatically by compose's own variable interpolation and substituted into Alertmanager's config at container start by `monitoring/alertmanager/docker-entrypoint.sh`.

### Prometheus Metrics
- `proxy_pool_validated_count` — proxies with score ≥40 (L1 threshold). Updated by `harvest_once()`.
- Additional metrics in `observability/metrics.py`.

### Alerts
- `ProxyPoolCriticallyLow`: fires when `proxy_pool_validated_count < 5` for 5 minutes. Severity: critical.
  **Round 34 note — a second, independent pool-health-alerting path now
  exists, deliberately, not as an unreconciled duplicate.**
  `proxy/pool_health.py::PoolHealthMonitor` computes its own per-tier
  HEALTHY/DEGRADED/CRITICAL state every health cycle and, when
  `config.webhook.ops_webhook_url` is set, pushes a `proxy_pool.critical`/
  `degraded`/`recovered` event straight to Slack via the new webhook-
  outbox/sweeper path (`.claude/knowledge/architecture.md` →
  "Notifications & Proxy Self-Healing") — event-driven, not a scraped-
  gauge-plus-duration rule like this one. Built independently of this
  rule, then reconciled same-day in a knowledge audit: **decision is to
  keep both**, since they fail independently (this rule needs Prometheus
  + a 5-minute sustained condition and goes dark if the app's own
  delivery pipeline breaks; the event-driven path needs that pipeline
  healthy and goes dark if Prometheus/Alertmanager are down) — each
  covers the other's blind spot. `config/base.yaml` recommends pointing
  `ops_webhook_url` at a different Slack channel than `SLACK_WEBHOOK_URL`
  so the two don't read as a confusing double-alert. Full reasoning:
  `.claude/knowledge/decisions.md` → "Keep Both Pool-Health Alert Paths".
- `CircuitBreakerFrequentTrips`, `DeadLetterQueueGrowing`, `CapSolverBudgetExhausted`, `ProxyExhaustionRateHigh`, `HighJobFailureRate`, `HighAPIErrorRate`, `PgBouncerPoolNearLimit`, `RedisUnreachable` — all defined and all now backed by real metrics (round 25 — see below).
- Rules in `monitoring/alerts/prometheus_rules.yml`. 11 rules as of round 25 (was 12 — `BrowserPoolExhausted` removed, see below), `promtool check rules` validated against a real Prometheus container.
- **`BrowserPoolExhausted` REMOVED (round 25), not fixed.** Its expr was
  `browser_pool_size{status="idle"} == 0`, but `BrowserPool` now lives inside
  the rq work-horse process for one job's lifetime (see
  `.claude/knowledge/architecture.md` → "Browser Pool") — that process never
  serves `/metrics` and is gone before the next scrape could see it. A
  `browser_pool_size` gauge set there would hit the exact cross-process
  problem the other round-25 metric fixes exist to close, with no real fix
  available short of a persistent (not per-job) worker pool. Revisit only if
  `BrowserPool`'s lifetime changes.
- **The other 7 were dead metrics too until round 25** — `circuit_state`
  (now `circuit_breaker_trips_total`, global not per-domain — Redis can't
  cheaply enumerate every domain), `dlq_size`, `capsolver_daily_spend`
  (now divided by a real per-tenant `capsolver_daily_ceiling`, not a
  hardcoded `1.0`), `proxy_exhausted_total` (labeled by `level`, not
  `domain` — unbounded-cardinality label removed), `job_duration_seconds_count`/
  `_sum`, `http_requests_total`. Mechanics: `.claude/knowledge/architecture.md`
  → "Metrics: Cross-Process Emission Pattern".
- **`PgBouncerPoolNearLimit`** now backed by a `pgbouncer_exporter` sidecar
  (`docker-compose.yml`, `prometheuscommunity/pgbouncer-exporter` image,
  reads real `SHOW POOLS`/`SHOW CONFIG` state — `stats_users = scraper`
  added to `infra/pgbouncer/pgbouncer.ini` for this). Metric names in the
  alert rule match that exporter's documented output but haven't been
  confirmed against a live scrape of the exporter itself yet — verify with
  `curl pgbouncer_exporter:9127/metrics` before trusting this alert in
  production.

### Alertmanager
- Two-tier Slack routing: `default` receiver → `#alerts` (repeat 4h), `paging-channel` receiver → `#alerts-critical` for severity=critical (repeat 30m).
- `send_resolved: true` on both receivers — resolution notifications dispatched.
- Webhook URL from `.env` (`SLACK_WEBHOOK_URL`), never committed. `docker-entrypoint.sh` substitutes via `sed` at container start.
- Global `slack_api_url` required (Alertmanager v0.33.1 — per-receiver `api_url` silently ignored).

### Distributed Tracing (round 24)
- Jaeger UI at `:16686` — query `/api/traces?service=scraper-engine` (optionally
  `&operation=<name>` or `&tag=job_id:<id>`) for programmatic verification
  instead of eyeballing the UI.
- Toggle: `observability.tracing_enabled`. Exporter target:
  `observability.otlp_endpoint` (default `http://jaeger:4317`).
- What's traced: every API request; one `scrape_job` span per rq job; one
  `proxy_daemon_{harvest,promotion,health,retention}` span per harvester
  cycle; every outbound httpx/Postgres/Redis call nested under whichever of
  those is active. Full architecture: `.claude/knowledge/architecture.md` →
  "Observability & Tracing".
- **Operator note:** an unreachable Jaeger adds up to ~2s latency per rq job
  (bounded `force_flush` + exporter timeout — see troubleshooting.md →
  "BatchSpanProcessor + fork()") rather than failing the job. Structured
  JSON logs (`observability.logging_level`) are independent of tracing and
  keep working even if Jaeger is down.

---

## CI Pipeline (Live)

**File:** `.github/workflows/test.yml` — 5 named jobs (`lint`/`unit`/
`integration`/`chaos`/`build-and-push`), but `unit`/`integration`/`chaos`
each run a `strategy.matrix.python-version: ["3.11", "3.12"]` (round 28), so
7 real check contexts report per run. GitHub Actions hosted, green as of
round 28 (PR #15).

**Jobs (each `needs:` the previous):**
- **lint:** `pip install -r requirements-dev-lock.txt` (single source of
  pinned versions, round 28 — see Known Operational Gaps #12) + a drift
  check (`uv pip compile --python-version 3.11 ...` into `/tmp`, diffed
  against the committed lockfiles, fails the build on drift) + `pip-audit
  -r requirements-lock.txt` (round 28, fails on known vulnerabilities) +
  `ruff check` + **mypy `--strict`** (baseline empty; fails on ANY error
  across `src/scraper_engine/{core,proxy,orchestrator,api,storage,fetcher,
  browser,observability}`) + grep-gates (no direct fetcher construction
  outside `factory.py`; `force_engine` never in production) +
  `tests/fixtures/challenge_mirror` ruff baseline + mypy-shrinkage
  advisory. Python 3.12 only — mypy/ruff don't need matrix coverage.
- **unit / integration:** `python-version` matrix (3.11 + 3.12, round 28);
  install is `pip install -r requirements-dev-lock.txt` +
  `pip install -e . --no-deps` (was a hand-listed ~40-package `pip install`
  line per job through round 27 — see Known Operational Gaps #12).
  `integration` additionally brings up `minio` via the project's own
  `docker compose` (round 28 — `tests/integration/test_s3_client.py`/
  `test_api_main.py` need a real S3-compatible endpoint; GitHub Actions
  service containers can't override a container's CMD, which `minio`'s
  image requires, so it can't be a bare `services:` entry like
  postgres/redis are).
- **chaos:** **Real PgBouncer, not GH Actions `services:`** (round 23) — a bare
  `services:` container pair can't produce the SCRAM-auth-off-a-live-`pg_authid`
  transaction pooling that G-05's test needs, so this job runs
  `docker compose up -d postgres redis pgbouncer minio` instead (reusing the
  project's own `pgbouncer-init` dependency chain from `docker-compose.yml`),
  polls `:6432` for TCP readiness, then runs the combined
  `tests/unit/ tests/integration/ tests/chaos/` suite with `--cov=
  src/scraper_engine --cov-fail-under=99 --cov-report=json:coverage.json`,
  followed by a second step running `tools/check_coverage_ratchet.py
  coverage.json` (round 28 wired the gate; round 62 split it in two). The
  other two jobs run without `--cov` since this job re-runs everything
  anyway with full infra up.

  **`--cov-fail-under` is NOT the gate and is deliberately not 100.**
  Round 62 enabled `branch = true`, which drops the blended figure to
  ~99.3%, so the percentage is a coarse safety net only. The real gate is
  the ratchet script: zero missed LINES (no tolerance, the pre-round-62
  guarantee unchanged) plus an absolute missed-BRANCH budget that may only
  ever shrink — the script fails if the count rises AND if it falls without
  `BRANCH_BUDGET` being lowered, so improvements get locked in. Anyone
  "fixing" the 99 back to 100 will break the build and silently re-hide the
  branch gaps. See Known Operational Gaps #14 and
  `.claude/knowledge/technical-debt.md`'s round-62 coverage audit.
- **build-and-push (round 22):** builds the root `Dockerfile`, pushes to GHCR
  (`ghcr.io/<owner>/<repo>:<sha>` and `:latest`) via the automatic
  `GITHUB_TOKEN` — no new secret needed. Gated `if: github.event_name ==
  'push' && github.ref == 'refs/heads/main'` — never runs on `pull_request`
  (a fork PR must never get registry write access), so it correctly shows
  `skipping` on every PR's checks and only actually runs after a merge to
  `main`. Publishes an image; does not deploy anywhere — no deploy target
  (k8s/systemd/cloud) exists in this repo yet.

**Run URL:** https://github.com/massiveconduct-boop/scraper-engine/actions

**Branch protection on `main`:** required status checks must list the exact
7 job-context names above (`lint`, `unit (3.11)`, `unit (3.12)`,
`integration (3.11)`, `integration (3.12)`, `chaos (3.11)`, `chaos (3.12)`),
not the bare job names — see Known Operational Gaps #15 for what happens
when this drifts.

**Still excluded from CI (by design, unchanged):**
- 2 Camoufox-dependent unit tests (binary ~300MB, run locally)
- L2/L3 live escalation tests (Camoufox + challenge mirror)
- `browser/` package's own coverage (needs real Firefox) — real local
  number checked round 28: 84%, not gated

**Historical note (superseded round 23):** `test_promotion.py` and
`test_pgbouncer_search_path_isolation.py` used to be permanently `--ignore`'d
in CI (`test_promotion.py` for a judge-server-subprocess concern that no
longer applies now that it uses `sys.executable` instead of a hardcoded
`"python"` binary; the PgBouncer test for lack of a real pooler in CI). Both
pass locally and, as of round 23, in real CI too — see the chaos job above.
Recorded here so a future reader doesn't waste time rediscovering why they
were once excluded.

---

## Scaling

| Component | Current | Scale strategy |
|---|---|---|
| API | 1 replica | Horizontal behind load balancer |
| Workers L1 | 1 | Scale first (most traffic) |
| Workers L2/L3 | 1 each | Scale based on escalation rate |
| Browser pool | 8 instances | `max_total_instances` in config; per-worker |
| PostgreSQL | Single | Read replicas for multi-tenant |
| Redis | Single | Sentinel for HA |
| Prometheus | 1 instance | Federation for multi-DC |
| Alertmanager | 1 instance | Cluster mode for HA |

---

## Config-Driven Timeouts

L2/L3 fetcher timeout values live in `config/production.yaml` under `levels.level_2` and `levels.level_3`. Not hardcoded in fetcher code:

```yaml
levels:
  level_2:
    goto_wait_until: "domcontentloaded"
    networkidle_timeout_ms: 5000
    max_total_wait_ms: 15000
  level_3:
    goto_wait_until: "load"
    post_load_fixed_wait_ms: 10000
    max_total_wait_ms: 30000
    retry_wait_increment_ms: 5000
```

---

## Known Operational Gaps

1. **PgBouncer pg_hba.conf:** Requires `host all all 172.0.0.0/8 md5` rule added to Postgres. Without it, auth_type must be scram-sha-256 in both pgbouncer.ini and pg_hba.conf.
2. **CAPTCHA solving (round 19 provider, round 20 wiring, round 22 root-caused):** NoCaptchaAI primary + CapSolver fallback (`services/`). ImageToText live-solved with real money; reCAPTCHA v2 / AntiTurnstileTask / GeeTest-v4(captchaId) / MTCaptcha are live-accepted (task created, no error) but never solved — root cause confirmed, see below. **Wired into the L2/L3 fetch path** (round 20): worker builds the solver once, `fetcher/_captcha.py` does DOM detect→solve→inject→re-poll, best-effort + null-safe (no key → solving skipped). DOM detect/inject live-verified end to end round 22 (real Camoufox, real DOM, real inject) — the one unproven step is a target accepting a solved token, blocked on the account gap below, not the mechanics.

   **Provider-key health is now observable (post-round-20, extended round-22).** A present key is
   NOT a working key — NoCaptchaAI can have no active plan and CapSolver keys can 401 or have $0 balance.
   Mechanisms that surface this:
   - `captcha_provider_configured{provider}` gauge — 1 if a key is present, 0 if
     absent (set at solver build, no network). Shows *missing* config in
     monitoring immediately.
   - `tools/validate_captcha_keys.py` — active preflight that calls each
     provider's balance endpoint and reports WORKING / NO PLAN / NO FUNDS /
     REJECTED / no-key (keys masked). Exit 0 if any provider works, 1 if none.
     `NoCaptchaAIClient.has_active_plan()` (round 22) calls the current
     `GET /balance` endpoint (richer than the legacy one `get_balance()` uses)
     and is what makes `NO PLAN` detectable automatically instead of silently
     reporting a misleading `WORKING`.

   **Operator runbook when captcha solving isn't producing tokens:**
   1. `set -a && . ./.env && set +a && .venv/bin/python tools/validate_captcha_keys.py`
      — confirm which provider is REJECTED/NO FUNDS/NO PLAN and why.
   2. `NO FUNDS` → top up that provider's balance. `REJECTED` → replace the key.
      `NO PLAN` (NoCaptchaAI) → **buy an actual package** at
      nocaptchaai.com/manage (pay-as-you-go packages, $10/50K solves+) — ad-hoc
      wallet top-ups do not grant worker-slot capacity, only a purchased
      package does (round-22 finding, confirmed against the live pricing page
      and the account's own `plan` object).
   3. Re-run the preflight (expect WORKING), then the full end-to-end check
      `tools/verify_captcha_live.py` — note that script targets Google's
      official reCAPTCHA demo page and can hang for its full ~120s poll
      ceiling even on a healthy account; prefer a real (non-demo) target when
      confirming a fix.
   **Current state (last preflight, round 22):** both keys *authenticate* —
   NoCaptchaAI balance ≈ $1.00 but **`NO PLAN`** (`plan.planType`/`planId`
   both empty, `is_default: 1` — wallet-only account, root-caused via raw
   API probing across two different real sitekeys, not a demo-key artifact
   and not outdated code — the request format matches NoCaptchaAI's current
   docs exactly), CapSolver balance **$0.00** (authenticates but can't pay
   for solves — top up to enable the fallback). `get_balance` proves the
   key/account, not plan/capability; `has_active_plan()` proves the plan;
   only a real solve proves the specific capability. All account/billing
   actions, not code. Full evidence: `.claude/knowledge/decisions.md` →
   "CAPTCHA Solver" round-22 follow-ups; `.claude/knowledge/troubleshooting.md`
   → "stuck idle forever".
3. **mypy `--strict` clean (RESOLVED, round 18):** was 23 baseline findings; all fixed. `strict = true` in pyproject, `mypy core/ proxy/ orchestrator/ api/ storage/ fetcher/ browser/ observability/` → "Success: no issues found in 57 source files". `tools/mypy-baseline.txt` is now empty and the CI gate fails on ANY error (no tolerance). Fixes included a real bug (`api/main.py` called `redis.close()`, which doesn't exist — the method is `stop()`); the rest were type precision (Protocol for the ASN classifier, `Any` for duck-typed Playwright pages/contexts, optional/generic args, a justified `type: ignore[no-untyped-call]` on the untyped `AsyncCamoufox`).
4. **Docker image ~4 GB:** Camoufox Firefox binary ~300 MB unavoidable (BD-02). Accepted as final for Oracle Cloud VPS (100 GB boot volume). Round 13 fixed the launch-lib chain (xvfb, libgtk-3-0, libx11-xcb1, camoufox[geoip]) — the image now actually launches a browser, not just ships one.
5. **`proxy-harvester` daemon (RESOLVED, post-round-20).** The container
   command was `python -m proxy.harvester`, which loaded a module with no
   `if __name__ == "__main__"` and exited 0 immediately — so the harvest /
   promotion / health routines were never scheduled and the container Exited(0)
   silently on every deploy (not a round-20 regression). Fixed by
   `proxy/harvester_daemon.py` (`python -m proxy.harvester_daemon`): a supervisor
   that builds `ProxyHarvester`, `ProxyPromotionJob`, and `HealthMonitor` from
   config and runs three independent timers (`harvest_once` @ `interval_seconds`
   600, `run_once` @ `promotion_interval_seconds` 900, `check_all` @
   `health_interval_seconds` 300), each isolating its own failures, with graceful
   SIGTERM/SIGINT shutdown. The CLI `harvest` command now runs one cycle manually
   (was a `"not yet implemented"` stub). Connection strings come from the single
   `StorageConfig` source (DB through PgBouncer).
6. **Stale Dockerfile Python pin (RESOLVED, round 14):** The committed Dockerfile text pinned `python:3.11-slim` from initial commit through round 12, but every *built* image (incl. the deployed `scraper_engine-api`) ran Python 3.12.13 — matching local venv (3.12.3) and CI (3.12). So the pin was documentation drift, never a runtime exposure; 3.11 was never deployed. Round 13 aligned the text to `python:3.12-slim`. Recorded here so the historical mismatch is explicit, not implicit in a diff.
7. **AWS WAF captcha unverified (BACKLOG — needs a real target).** The solver supports AWS WAF (`solve_aws_waf`) but it has never been exercised against a live AWS-WAF-protected site (per-request runtime data can't be mocked faithfully). To verify when a target is available: point `tools/verify_captcha_live.py` at the target, confirm DOM detection extracts the AWS WAF challenge and that a solved token is accepted. Low priority.
8. **`BrowserPool` unwired (RESOLVED round 25).** Was: every L2/L3 fetch a
    cold-start browser launch, no prewarming, no reuse. Now wired — one pool
    per rq job, leased by `Level2Fetcher`/`Level3Fetcher` via
    `fetcher/factory.py`. Also fixed a real correctness bug found while
    wiring it in (mismatch used to destroy live browsers instead of keeping
    them pooled). Full story: `.claude/knowledge/technical-debt.md` (round
    25); `.claude/knowledge/architecture.md` → "Browser Pool".
9. **CapSolver budget was a single hardcoded global ceiling (RESOLVED round
    25).** `CapSolverBudget` now reads the real per-tenant DB column
    (`tenants.capsolver_daily_credit_ceiling`); `config.capsolver.
    max_concurrent_solves` now sizes `CAPSOLVER_CONCURRENCY` for real.
    (`daily_credit_ceiling_default` was removed from config — superseded by
    the DB column, kept as the single source of truth.) Also fixed a real
    bug found alongside it: `_spend_key()` ignored `tenant_id`, pooling every
    tenant's spend into one Redis key regardless of ceiling. Full story:
    `.claude/knowledge/technical-debt.md` (round 25).
10. **Camoufox config entirely ignored (RESOLVED round 25).**
    `geoip`/`humanize`/`headless_mode` now flow into `CamoufoxWrapper`'s
    constructor from config; `max_total_instances` now sizes
    `BROWSER_SEMAPHORE` via a new `configure_budget()` called once at rq
    worker process startup. Full story: `.claude/knowledge/technical-debt.md`
    (round 25).
11. **`fetcher/botasaurus_wrapper.py` — deleted, then restored for real
    (RESOLVED round 25).** Was orphaned (never imported, `botasaurus` not a
    dependency). Deleted first as dead code, then restored and wired for
    real per an explicit follow-up ask (the authoritative spec §3.6
    designs a real implementation). `level_2.engine` config now genuinely
    reflects what runs. Full story + the reversal reasoning:
    `.claude/knowledge/technical-debt.md` (round 25);
    `.claude/knowledge/decisions.md` → "Botasaurus".
12. **Dependency declarations live in 5 separate places, none read from each
    other (RESOLVED round 28 — see `.claude/knowledge/technical-debt.md`,
    round 28, for the full story).** `requirements-lock.txt` (runtime) /
    `requirements-dev-lock.txt` (+dev extras), generated via `uv pip
    compile --python-version 3.11 --no-header`, are now the single source
    every job and the Dockerfile install from; a `lint`-job step
    regenerates both into `/tmp` and diffs against committed, failing the
    build on drift. **`--python-version 3.11` is load-bearing, not
    cosmetic** — without it, `uv` resolves against whatever interpreter
    ran the compile, and can silently pick a version that doesn't support
    `requires-python`'s floor (`numpy==2.5.1`, needs Python >=3.12, broke
    the `unit (3.11)` CI job the first time this lockfile setup shipped,
    round 28 — caught by real CI, not local testing, since this box's dev
    venv is 3.12). Regenerate with the exact command in `CONTRIBUTING.md`,
    not a bare `uv pip compile`.
    Original finding (kept for context — the drift class this closes):
    `pyproject.toml`'s `dependencies` list, the Dockerfile's `deps` stage
    (a hardcoded `RUN pip install ...` line, "mirrors .github/workflows/
    test.yml" per its own comment — but only by convention, not by any
    actual mechanism), and 3-4 separate hardcoded install lists inside
    `.github/workflows/test.yml`'s different jobs. Adding a real dependency
    to `pyproject.toml` alone does **not** make it reach the Docker image or
    CI — found round 25 when `botasaurus` was added to `pyproject.toml` but
    the built image didn't actually contain it until the Dockerfile and
    every CI job's install list were separately updated too.
    **Round 27: this exact drift class caused a real CI failure.** CI's
    lint job installs `mypy` unpinned and never installs `types-redis` at
    all, while local dev extras pinned `mypy==2.3.0` + `types-redis>=4.6.0`
    (a stub package targeting a redis-py version 4 majors behind the
    installed 8.0.1). With both present, local mypy resolved `redis.
    asyncio.Redis` via the stale third-party stub instead of the real
    package's own `py.typed` inline types — demanding a generic type
    argument CI's mypy (correctly, using the real types) rejected. Fixed by
    removing `types-redis` from `pyproject.toml`'s dev extras (confirmed via
    `git stash` that the underlying conflict predated the round-27 change
    that exposed it) — that patched the one symptom; the structural fix
    (single lockfile, everything installs from it) is the round-28 work
    described above. **Operator checklist when adding any new Python
    dependency:** update `pyproject.toml`, regenerate both lockfiles with
    the `CONTRIBUTING.md` command (`--python-version 3.11`, not a bare `uv
    pip compile`), then `pip install -r requirements-dev-lock.txt` +
    `python -c "import <pkg>"` locally to actually confirm it resolves —
    don't trust that editing `pyproject.toml` alone did anything.
13. **`proxy_source_healthy` had the same cross-process gap as the round-25
    alert metrics (RESOLVED round 25 follow-up).** Set inside the
    `proxy-harvester` container, invisible to the `api` process's
    `/metrics`. Now written to Redis at harvest time, refreshed into the
    gauge at scrape time. Missed by the original round-25 audit (the Gauge
    object exists and is called somewhere, so a naive check doesn't catch
    it) — found only via a live `/metrics` cross-check after the rest of
    round 25 landed. Full story: `.claude/knowledge/technical-debt.md`
    (round 25 follow-up).
    Full finding: `.claude/knowledge/technical-debt.md` (round 24).
14. **Coverage gate was dead config (RESOLVED round 28; SCOPE CORRECTED
    round 62 — see below).**
    `[tool.coverage.report] fail_under` was declared (90, then 100) but
    never enforced — none of the three pytest invocations in
    `.github/workflows/test.yml` passed `--cov`. Real measured coverage at
    the time: 72% once `include` was corrected to match `[tool.coverage.
    run] source`'s 8 packages (was silently only gating 3). Same bug class
    as #8-11 above (config/gate declared, nothing actually calls it) — the
    sixth occurrence of this exact pattern in this codebase's history, and
    the reason round 28 treated it as the top priority rather than another
    one-off patch. Now wired into the `chaos` job (see CI Pipeline above)
    and brought to 100% across every package in scope except `browser/`
    (documented exclusion, needs real Firefox). A round-34 knowledge audit
    caught a real regression to 97.91% (round 34 shipped 3 daemon `run()`
    functions and 2 single lines untested, plus regressed
    `harvester_daemon.py` from 100%) and closed it same-day — real
    measured coverage as of the fix is 99.57%, gate passes except for
    `services/botasaurus_requests_client.py` (56%, confirmed pre-existing,
    aarch64-sandbox-only, not a CI blocker on the x86_64 runners — see
    `.claude/knowledge/technical-debt.md`'s round-34 "Coverage gap" entry).
    Full story + the other 7
    findings closed alongside it: `.claude/knowledge/technical-debt.md`
    (round 28).
15. **CI job matrix changes can silently break branch protection (RESOLVED
    round 28, found post-merge while watching real CI).** Adding
    `strategy.matrix.python-version` to `unit`/`integration`/`chaos`
    changed their reported check names from `unit`/`integration`/`chaos`
    to `unit (3.11)`/`unit (3.12)`/etc. `main`'s branch protection
    `required_status_checks.contexts` still listed the old bare names —
    GitHub has no way to reconcile "a required check that will never exist
    again" with "these new checks that did run and passed," so it left the
    PR permanently `mergeStateStatus: BLOCKED` even with all 7 real checks
    green (`gh pr merge` reported "not mergeable" with no explanation
    pointing at this — surfaced only by fetching
    `required_pull_request_reviews.required_approving_review_count` (0,
    ruling out a review block) and `required_status_checks.contexts`
    directly via `gh api repos/.../branches/main/protection`). Fixed by
    `PATCH`ing the protection rule's `required_status_checks` to the 7
    real context names (`lint`, `unit (3.11)`, `unit (3.12)`,
    `integration (3.11)`, `integration (3.12)`, `chaos (3.11)`,
    `chaos (3.12)`). **Operator checklist when adding/removing a
    `strategy.matrix` on any required job:** update `main`'s branch
    protection required status checks in the same change — a passing CI
    run is not sufficient evidence the PR is actually mergeable; check
    `gh pr view <n> --json mergeStateStatus` too.
