# Knowledge Catalog

Index of all knowledge documents. Read this first to discover what exists
before loading context. **This file is read every session — keep it a
lean index, not a narrative.** Full purpose/scope/keywords/dependencies/
related-docs metadata lives in each document's own header (first ~6 lines
of every file in `.claude/knowledge/`), not duplicated here — open the
document once it looks relevant, don't expect the full picture from this
table alone.

## Architecture & Design

| Document | Purpose | When to read |
|---|---|---|
| `.claude/knowledge/architecture.md` | System design, invariants, module interactions, data flow | Understanding how components connect; adding new modules |
| `.claude/knowledge/decisions.md` | Design decisions with rationale, tradeoffs, rejected alternatives | Understanding WHY something was built a certain way; considering changes |
| `.local/specs/scraper-engine-blueprint-v2.md` (local-only, not tracked in git) | Authoritative specification v2.0 | Source of truth for requirements and invariants |

## Implementation

| Document | Purpose | When to read |
|---|---|---|
| `.claude/knowledge/standards.md` | Coding conventions, test patterns, report format, lint rules | Writing new code, tests, or reports |
| `.claude/knowledge/troubleshooting.md` | Known bugs, diagnostic patterns, common failures and fixes | Debugging failures; encountering a familiar error pattern |

## Operations

| Document | Purpose | When to read |
|---|---|---|
| `.claude/knowledge/operations.md` | Deployment, infrastructure, CI, monitoring, alert config | Deploying; setting up CI; configuring alerts |
| `docs/guides/deployment.md` | Production deployment guide with scaling, security, troubleshooting | First-time deployment; production incidents |

## History & Evidence

| Document | Purpose | When to read |
|---|---|---|
| `.archive/closure/ROUND-6-DEFINITIVE.md` | Consolidated round 6 evidence — all 6 items, 10 bugs fixed | Auditing claims; understanding what was resolved |
| `.archive/other/round-6-double-issue-fix.md` | `acquire()` double-issue bug — root cause, fix, regression tests | Understanding pool safety; similar concurrency bugs |
| `.archive/closure/round-6-exit-144-closure.md` | Exit 144 investigation — Bash tool timeout, production timeout answer | Understanding signal 144 in CI; timeout debugging |
| `.archive/evidence/round-6-broker-diagnostic.md` | Broker subprocess diagnostic — works, exit 0, 3 proxies | Debugging proxybroker2; harvest pipeline issues |
| `.archive/other/round-6-critical-fixes.md` | ON CONFLICT restore, hot-browser pool, Prometheus gauge | Understanding the three critical fixes from final review round |
| `.archive/other/round-6-lease-fix.md` | `lease()` async context manager — invariant §1.1.6 restoration | Understanding pool safety contract |
| `.archive/closure/final-production-readiness-report.md` | Comprehensive production readiness (round 5) | Overall project status |
| `.archive/evidence/round-7-evidence-report.md` | Session isolation (Postgres), proxy promotion (attempt tracking), alert wiring (Slack) | Round 7 deliverables + evidence |
| `.archive/closure/round-8-deliverables.md` | Debug endpoint deletion, pool.py full trace, cookie persistence, deps pinned, api/routes.py wired, per-tenant quota | Round 8 deliverables |
| `.archive/closure/round-8-closure-evidence.md` | Quota enforcement fix — exception-based, per-tenant limits, three-curl evidence | Quota implementation details |
| `.archive/other/per-tenant-quota-enforcement.md` | Per-tenant quota curl evidence (system=2, other=5) | Two-tenant isolation verification |
| `.archive/evidence/round-9-evidence-report.md` | Camoufox binary confirmed, CI pipeline (4-stage green), L2/L3 page.content() race fix, mypy --strict findings | Round 9 deliverables |
| `.archive/evidence/round-10.03-ratchet-proven.md` | mypy ratchet gate proven on real CI (probe file caught, exit 1, reverted) | Ratchet mechanism verification |
| `.archive/evidence/round-11-evidence.md` | Force-push recovery, all 6 bugs fixed, 209 collected/203 passed/6 skipped/0 failed, config-driven timeouts | Final round closure |
| `.archive/closure/round-12-final.md` | Force-push root cause (`git reset --hard`), branch protection, `v1.0.0-rc1` tag, ChallengeDetector + `_safe_content` guard | Rounds 12–12.4 consolidated |
| `.archive/evidence/round-13-evidence.md` | Config DI factory + CI gate, `force_engine` negative-control seam, monitoring dashboard/alerts (Slack-proven), per-source health gauge, ruff 45→0, mirror ruff baseline, Docker multi-stage + launch-lib chain fix | Round 13 deliverables |
| `.archive/evidence/round-14-evidence.md` | L2 flakiness fixed (shared `poll_until_solved` retry loop, deterministic A/B), host-vs-container 202/201 reconciled (pgbouncer test), Python 3.11 never-deployed (stale pin) | Round 14 deliverables |
| `.archive/evidence/round-15-evidence.md` | Real-target validation (books/quotes/scrapethissite/webscraper/nowsecure/sannysoft/scrapecups) — Cloudflare passed, no webdriver leak; `HOST_UNREACHABLE` non-retryable DNS category added | Round 15 real-site validation + DNS taxonomy fix |
| `.archive/evidence/round-16-evidence.md` | Infinite-scroll/lazy-load `autoscroll` (consecutive-stable stop; live-proven 10→30 quotes) | Scroll handling |
| `.archive/evidence/round-17-evidence.md` | Full-stack e2e smoke (auth/SSRF/quota/persist/retrieve); GET /v1/jobs 500 fix (asyncpg UUID→str) | Live API pipeline + UUID bug |
| `.archive/evidence/round-19-evidence.md` | CAPTCHA solver — NoCaptchaAI primary/CapSolver fallback; ImageToText solved live; provider-specific task-type corrections (docs stale) | CAPTCHA solving subsystem |
| `.archive/evidence/round-20-evidence.md` | CAPTCHA solver wired into L2/L3 fetch path — `fetcher/_captcha.py` (DOM detect→solve→inject→re-poll), worker builds solver once, factory threads it, best-effort/null-safe, 15 tests | CAPTCHA fetch-path integration |
| `.archive/closure/comprehensive-phase-report.md` | Challenge mirror + chaos tests (9/9 pass), CI pipeline setup | Infrastructure phase |
| `.archive/evidence/ci-pipeline-evidence.md` | CI pipeline run URL + job statuses | CI verification |
| `.github/workflows/test.yml` | Live CI — 5 named jobs, 7 real check contexts (`unit`/`integration`/`chaos` each run a Python 3.11+3.12 matrix, round 28): lint (mypy-strict + fetcher-factory + force_engine grep-gates + challenge-mirror ruff baseline + lockfile drift-check + pip-audit, round 28), unit, integration (+minio, round 28), chaos (real PgBouncer via `docker compose`, not GH `services:` — round 23; also where the real 100%-coverage gate is enforced, round 28), build-and-push (GHCR, `push` to `main` only — round 22). See `.claude/knowledge/operations.md` → "CI Pipeline (Live)" for full detail, and Known Operational Gaps #15 for why `main`'s branch protection required-checks list must be kept in sync with these exact context names. | CI configuration reference |
| `tools/mypy-baseline.txt` | EMPTY since round 18 — mypy `--strict` clean; CI fails on any error | mypy strict gate |

## Technical Debt & Round History

Full round-by-round technical debt log — every gap found, every fix, every
open thread — lives in a dedicated document, not inline here (moved out in
the round-28 knowledge-architecture audit; was 897 lines of this file,
force-loaded every session regardless of relevance).

| Document | Purpose | When to read |
|---|---|---|
| `.claude/knowledge/technical-debt.md` | Complete round-by-round history — every bug found, every decision, every open thread, from project inception to the current round | Investigating whether something was already fixed; needing the full story behind a "RESOLVED (round N)" reference; auditing a specific round's changes |

**Current state, for a quick orientation without opening that file:** as of
round 34, proxy pool exhaustion now self-heals (event-driven harvest
trigger, not just a timer) and transient DLQ entries (`PROXY_EXHAUSTED`,
`CIRCUIT_OPEN`) auto-retry once their condition clears; webhook delivery
is a durable transactional outbox with a real Slack Block Kit formatter
instead of fire-and-forget raw JSON; `JobStatusResponse.partial_failure`
stops a job with a DLQ'd URL from reporting as a clean `COMPLETED`; the
webhook URL is now SSRF-guarded like any scrape target. **Both open
threads from round 34 are now resolved** (same-day knowledge audit +
follow-up): (1) the pool-health-to-Slack path (`ops_webhook_url`) and the
pre-existing `ProxyPoolCriticallyLow` Alertmanager rule are kept as
deliberately independent, complementary alert paths — not merged, not
reconciled into one — see `decisions.md` → "Keep Both Pool-Health Alert
Paths"; (2) the coverage gate regression the audit found (97.91%,
`webhook_sweeper.py`/`dlq_reaper.py`'s `run()` functions untested,
`harvester_daemon.py` regressed from 100%) is fixed — 99.57% now, every
round-34 file at 100%, only a pre-existing aarch64-sandbox-only gap
remains (`botasaurus_requests_client.py`, not a CI blocker). See
`technical-debt.md`'s round-34 entry for both. Rounds 30–33
were not backfilled into `technical-debt.md` (see that file's header note)
— only round 29 and round 34 have full narrative entries there, though
scattered round-32/33 references exist in `architecture.md`/`decisions.md`.
Round 29's 8 caller-facing gaps (failed URLs no longer vanishing from job
results, `html_snapshot_url` reaching callers, job cancellation,
`Idempotency-Key` retry-safety, real per-URL progress, `Retry-After` on
429s) plus its 7-day scrape-result cache and generalized markdown
conversion still stand as documented. Schema-driven extraction accepting
multiple input formats remains explicitly deferred, not forgotten. Full
detail, as always, in `technical-debt.md` above.

## Reference

| Document | Purpose | When to read |
|---|---|---|
| `docs/reference/api-reference.md` | API endpoint reference (scrape, crawl, jobs, jobs/dlq, jobs cancel, health) — rewritten round 29 to match real behavior, previously described endpoints that never existed; round 34 added `partial_failure`, `auto_retry_count`, webhook SSRF-guard, and corrected the non-retryable-categories claim (proxy_exhausted/circuit_open are now auto-retried) | Integrating with the API |
| `.archive/evidence/auditable-verification-report.md` | Auditable report from round 4 | Historical reference |
| `.archive/directive/*.md` (11 files, uncataloged individually) | Original task directives issued per round (round 6 through round 14) — the ask, not the outcome; outcomes are the `evidence`/`closure` files above and `technical-debt.md` | Understanding what was originally requested for a given round, distinct from what was delivered |
| `.archive/closure/*.md` (7 more beyond the rows above, uncataloged individually — round-6 closure variants: `-closure`, `-final-admission`, `-final-report`, `-final-response`, `-report`, `-report-complete`, `-closing-items`; plus `round-8-closure-directive.md`, `final-round-report.md`, `production-readiness-report.md`) | Round-6 closure went through several iterations before `ROUND-6-DEFINITIVE.md` (cataloged above) became the actual consolidated version — these are its drafts/precursors | Only if `ROUND-6-DEFINITIVE.md` itself references one by name and you need the precursor's exact wording |
| `.archive/evidence/*.md` (12 more beyond the rows above, uncataloged individually — round 6/8/10/10.02/12/12.1-12.4 evidence, `auditable-report-review-round3.md`, `proxybroker2-resolution-report.md`, `resolved-issues-report.md`) | Per-round raw evidence for rounds not otherwise summarized elsewhere in this table | Auditing a specific early round in detail; `technical-debt.md` is the summarized version, these are the underlying raw evidence |
| `.archive/other/*.md` (5 files, uncataloged individually — `CLAUDE.original.md`, `node_real_js_verify.js`, `round-10.01-mypy-ratchet.md`, `round-7-implementation-plan.md`, `round-13-implementation-plan.md`) | Miscellaneous — a pre-rewrite CLAUDE.md snapshot, a JS verification script, and two rounds' implementation plans | Rarely — historical curiosity or if a specific old plan's original scope is in question |

## Update Policy

- Add new documents to this catalog when created.
- Remove or mark superseded when replaced.
- Each catalog entry must have: purpose, scope, when to read, related documents.
- Documents in `.claude/knowledge/` are permanent institutional knowledge. Documents in `docs/` are evidence artifacts.
