# Coding Standards & Patterns

**Purpose:** Conventions for writing code, tests, and reports in this project.
**Scope:** All new code, tests, reports, commits.
**When to read:** Before writing any code, test, or report.
**Related:** `.claude/knowledge/architecture.md`, `.claude/knowledge/decisions.md`

---

## Code Conventions

### Invariant Compliance (Checking for F-02)
Every import in `if TYPE_CHECKING:` must ONLY be used in type annotations (never in function bodies). If a symbol is called/instantiated at runtime, it needs a real import. **Audit checklist:** grep for `if TYPE_CHECKING`, find each imported symbol, search for runtime usage. This bug class (clean lint, clean types, `NameError` at runtime) has occurred 3 times (level_2.py, level_3.py, pool.py).

### Context Managers
Every resource acquisition must have guaranteed release (invariant §1.1.6). Use `async with` or `try/finally`. Never expose raw acquire/release pairs as the public API — wrap in a context manager.

### Column Names
`proxy_pool` schema uses `anonymity_level`, not `anonymity`. Check column names against the schema before writing INSERT/UPDATE statements. Use `SELECT column_name FROM information_schema.columns WHERE table_name='proxy_pool'` to verify.

### ON CONFLICT
The `proxy_pool` table has `UNIQUE (ip, port, protocol)`. All INSERTs must use `ON CONFLICT (ip, port, protocol) DO UPDATE SET`. Use `GREATEST` for score merge, `CASE WHEN` for conditional field updates.

### Prometheus Gauges
When adding a Prometheus gauge: define it in `observability/metrics.py`, update it in the relevant code path with `.set()`, create an alert rule in `monitoring/alerts/prometheus_rules.yml` that thresholds against the gauge metric (not row-level SQL). PromQL cannot evaluate SQL predicates.

---

## Test Patterns

### Unit Tests
- Mock external dependencies (Postgres, Redis, browser). Use `AsyncMock` / `MagicMock`.
- Test the contract, not the implementation. "Does acquire() return distinct objects?" not "Is the classify loop iterating correctly?"
- Add regression tests for every bug fixed. Name the bug in the test docstring.
- Tests should run without Camoufox/Docker when possible.

### Integration Tests
- Require Docker (Postgres, Redis, PgBouncer).
- Start infrastructure: `docker compose up -d postgres redis pgbouncer && alembic upgrade head`.
- Verify migration state before running: `alembic current` must equal `alembic heads`.
- Test real database interactions, schema creation, concurrency.
- Tests that mutate global tables (`DELETE FROM proxy_pool`) against the live DB are documented as a known risk. Acceptable on disposable CI instances; not acceptable if DB is shared.

### Chaos Tests
- Race conditions: multi-worker politeness, PgBouncer isolation, resource exhaustion.
- OS-subprocess tests for scheduling behavior (not just asyncio tasks).

### Live Tests (`tests/live/`)
- Require internet (httpbin.org) or Docker mirror (challenge-mirror).
- Camoufox-dependent tests are SKIPPED in pytest (binary import triggers heavy loading).
- Run standalone via `python -c` when Camoufox available.

---

## Report Standards

### Mandatory Structure
Every report must have: Header Metadata (date, spec ref), Environment & Infrastructure, Artifact Index, Per-Item Breakdown, Summary Matrix, Final Summary.

### Evidence
- Raw terminal output in code blocks — never retype or paraphrase.
- Exact commands that an auditor can copy-paste.
- No transient version numbers (Git HEAD, commit count) — they change on every commit. Use stable references instead.
- Every code block in a report must match its source file EXACTLY (byte-level). Verify with `diff`.

### Honest Limitations
- Per-item limitation subsections naming specific blocking conditions and decision owners.
- Separate objective fact from interpretation. Mark speculation as such.
- "Documented as limitation" is NOT closure — it's a placeholder.

### mypy Baseline Management
- `tools/mypy-baseline.txt` contains known type findings (23 entries). Committed to repo.
- CI ratchet step diffs current mypy output (`grep "^error:"` lines) against baseline via `comm -13`.
- Any NEW error beyond baseline fails the build. Known findings are advisory.
- `mypy==2.3.0` pinned in `pyproject.toml` — no version drift between local and CI.
- PRs touching files in the baseline should resolve those entries, shrinking the baseline over time.
- Local and CI produce different finding counts due to different stub resolution (pydantic, starlette versions). Baseline is CI-specific.

### Banned Patterns
- Paraphrased commands (`python -c "harvest + pool query"` instead of actual code)
- Stale numbers (HEAD, commit count) that change on next commit
- "X is functionally equivalent to Y" without test evidence
- OOM diagnosis without `free -h` + `dmesg` evidence
- Removing broken code to fix an error (restore ON CONFLICT, don't delete it)
- Code blocks that differ from actual source files

---

## Commit Conventions

- Conventional Commits format: `fix:`, `feat:`, `docs:`, `test:`, `chore:`
- Reference issue/gap IDs in body (F-02, G-05, BD-01)
- Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
- Subject ≤50 chars, body only when "why" isn't obvious from the diff
