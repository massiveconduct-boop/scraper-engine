# Knowledge Catalog

Index of all knowledge documents. Read this first to discover what exists before loading context.

## Architecture & Design

| Document | Purpose | When to read |
|---|---|---|
| `.claude/knowledge/architecture.md` | System design, invariants, module interactions, data flow | Understanding how components connect; adding new modules |
| `.claude/knowledge/decisions.md` | Design decisions with rationale, tradeoffs, rejected alternatives | Understanding WHY something was built a certain way; considering changes |
| `specs/scraper-engine-blueprint-v2.md` | Authoritative specification v2.0 | Source of truth for requirements and invariants |

## Implementation

| Document | Purpose | When to read |
|---|---|---|
| `.claude/knowledge/standards.md` | Coding conventions, test patterns, report format, lint rules | Writing new code, tests, or reports |
| `.claude/knowledge/troubleshooting.md` | Known bugs, diagnostic patterns, common failures and fixes | Debugging failures; encountering a familiar error pattern |

## Operations

| Document | Purpose | When to read |
|---|---|---|
| `.claude/knowledge/operations.md` | Deployment, infrastructure, CI, monitoring, alert config | Deploying; setting up CI; configuring alerts |
| `docs/deployment.md` | Production deployment guide with scaling, security, troubleshooting | First-time deployment; production incidents |

## History & Evidence

| Document | Purpose | When to read |
|---|---|---|
| `docs/ROUND-6-DEFINITIVE.md` | Consolidated round 6 evidence — all 6 items, 10 bugs fixed | Auditing claims; understanding what was resolved |
| `docs/round-6-double-issue-fix.md` | `acquire()` double-issue bug — root cause, fix, regression tests | Understanding pool safety; similar concurrency bugs |
| `docs/round-6-exit-144-closure.md` | Exit 144 investigation — Bash tool timeout, production timeout answer | Understanding signal 144 in CI; timeout debugging |
| `docs/round-6-broker-diagnostic.md` | Broker subprocess diagnostic — works, exit 0, 3 proxies | Debugging proxybroker2; harvest pipeline issues |
| `docs/round-6-critical-fixes.md` | ON CONFLICT restore, hot-browser pool, Prometheus gauge | Understanding the three critical fixes from final review round |
| `docs/round-6-lease-fix.md` | `lease()` async context manager — invariant §1.1.6 restoration | Understanding pool safety contract |
| `docs/final-production-readiness-report.md` | Comprehensive production readiness (round 5) | Overall project status |
| `docs/round-7-evidence-report.md` | Session isolation (Postgres), proxy promotion (attempt tracking), alert wiring (Slack) | Round 7 deliverables + evidence |
| `docs/round-8-deliverables.md` | Debug endpoint deletion, pool.py full trace, cookie persistence, deps pinned, api/routes.py wired, per-tenant quota | Round 8 deliverables |
| `docs/round-8-closure-evidence.md` | Quota enforcement fix — exception-based, per-tenant limits, three-curl evidence | Quota implementation details |
| `docs/per-tenant-quota-enforcement.md` | Per-tenant quota curl evidence (system=2, other=5) | Two-tenant isolation verification |
| `docs/round-9-evidence-report.md` | Camoufox binary confirmed, CI pipeline (4-stage green), L2/L3 page.content() race fix, mypy --strict findings | Round 9 deliverables |
| `docs/round-10.03-ratchet-proven.md` | mypy ratchet gate proven on real CI (probe file caught, exit 1, reverted) | Ratchet mechanism verification |
| `docs/round-11-evidence.md` | Force-push recovery, all 6 bugs fixed, 209 collected/203 passed/6 skipped/0 failed, config-driven timeouts | Final round closure |
| `docs/round-12-final.md` | Force-push root cause (`git reset --hard`), branch protection, `v1.0.0-rc1` tag, ChallengeDetector + `_safe_content` guard | Rounds 12–12.4 consolidated |
| `docs/round-13-evidence.md` | Config DI factory + CI gate, `force_engine` negative-control seam, monitoring dashboard/alerts (Slack-proven), per-source health gauge, ruff 45→0, mirror ruff baseline, Docker multi-stage + launch-lib chain fix | Round 13 deliverables |
| `docs/round-14-evidence.md` | L2 flakiness fixed (shared `poll_until_solved` retry loop, deterministic A/B), host-vs-container 202/201 reconciled (pgbouncer test), Python 3.11 never-deployed (stale pin) | Round 14 deliverables |
| `docs/round-15-evidence.md` | Real-target validation (books/quotes/scrapethissite/webscraper/nowsecure/sannysoft/scrapecups) — Cloudflare passed, no webdriver leak; `HOST_UNREACHABLE` non-retryable DNS category added | Round 15 real-site validation + DNS taxonomy fix |
| `docs/comprehensive-phase-report.md` | Challenge mirror + chaos tests (9/9 pass), CI pipeline setup | Infrastructure phase |
| `docs/ci-pipeline-evidence.md` | CI pipeline run URL + job statuses | CI verification |
| `.github/workflows/test.yml` | Live 4-stage CI pipeline (lint/unit/integration/chaos) | CI configuration reference |
| `tools/mypy-baseline.txt` | 23 known mypy findings, ratchet-protected | mypy regression prevention |

## Reference

| Document | Purpose | When to read |
|---|---|---|
| `docs/api-reference.md` | API endpoint reference (scrape, jobs, health, admin) | Integrating with the API |
| `docs/auditable-verification-report.md` | Auditable report from round 4 | Historical reference |

## Update Policy

- Add new documents to this catalog when created.
- Remove or mark superseded when replaced.
- Each catalog entry must have: purpose, scope, when to read, related documents.
- Documents in `.claude/knowledge/` are permanent institutional knowledge. Documents in `docs/` are evidence artifacts.
