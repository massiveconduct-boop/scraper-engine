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
