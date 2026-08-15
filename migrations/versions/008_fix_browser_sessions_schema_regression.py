# migrations/versions/008_fix_browser_sessions_schema_regression.py
"""Fix browser_sessions — migrations 004/005/007 silently reverted 002's fix.

Round 42. Migration 002 corrected create_tenant_schema()'s browser_sessions
table from its broken 001 shape (session_id, state, created_at, updated_at)
to the shape browser/session_state.py actually reads/writes (session_id,
domain, storage_state, last_used_at, expires_at). But 004, 005, and 007 each
redefine create_tenant_schema() wholesale (CREATE OR REPLACE FUNCTION, full
body) to add their own unrelated columns/tables, and each one's
browser_sessions block was copy-pasted from the *original* 001 definition,
not 002's fix — silently reverting it every time one of them ran. By 007
(the currently-installed function on every deployment that has run migrations
to head), any tenant schema created afterward gets the broken table back.

Live-caught (round 42): a real L3 fetch crashed with
`asyncpg.exceptions.UndefinedColumnError: column "storage_state" does not
exist` from browser/session_state.py's SessionStateManager.load() (called
unconditionally by browser/pool.py::BrowserPool.acquire() on any Camoufox
cold-start for a domain not already warm in that job's pool — effectively
every first L3 attempt per domain per job, and any L2 attempt whose
Botasaurus first-try failed and fell back to Camoufox). Session-state
persistence (SessionStateManager.save/load/delete) and
proxy/retention_reaper.py's expired-session cleanup have therefore been
completely non-functional since whichever of 004/005/007 first regressed
this, on every tenant schema created since — but the crash was invisible in
practice: BrowserPool.lease()'s save() call is wrapped in a try/except that
only logs a warning, and worker.py's escalation loop (pre-round-42 fix, see
that round's entry) mislabeled the resulting BROWSER_CRASH as a generic
"proxy_exhausted / All fetch levels exhausted" once it propagated through
every level — indistinguishable from genuine proxy pool exhaustion in the
DLQ, which is exactly what hid this for as long as it went unnoticed.

Every current tenant schema's browser_sessions table holds no usable data
under the broken shape: both the save() and load() paths would fail against
it (INSERT into a nonexistent `domain`/`storage_state` column would raise
the same UndefinedColumnError load() does), so nothing could have ever been
successfully persisted to reach here — a plain DROP + recreate (mirroring
002's own already-established approach for this exact table) is safe, not a
data-loss risk, and simpler than a defensive column-migrating ALTER for data
that structurally cannot exist.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "008"
down_revision: str | None = "007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ── Redefine create_tenant_schema() with the corrected browser_sessions
    # block — identical to 007's current definition otherwise. ──
    op.execute("""
        CREATE OR REPLACE FUNCTION create_tenant_schema(tenant_slug text) RETURNS void AS $$
        DECLARE
            safe_slug text;
        BEGIN
            IF tenant_slug !~ '^[a-z][a-z0-9_]{2,62}$' THEN
                RAISE EXCEPTION 'invalid tenant_id: %', tenant_slug;
            END IF;
            safe_slug := quote_ident(tenant_slug);

            EXECUTE format('CREATE SCHEMA IF NOT EXISTS %s', safe_slug);

            -- scrape_jobs (+ idempotency_key, round 29)
            EXECUTE format($f$
                CREATE TABLE IF NOT EXISTS %s.scrape_jobs (
                    job_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    urls TEXT[] NOT NULL,
                    config_used JSONB NOT NULL DEFAULT '{}',
                    status VARCHAR(20) NOT NULL DEFAULT 'PENDING'
                        CHECK (status IN (
                            'PENDING','PROCESSING','COMPLETED','FAILED','CANCELLED','DEAD_LETTER'
                        )),
                    webhook_url TEXT,
                    idempotency_key TEXT,
                    created_at TIMESTAMPTZ DEFAULT NOW(),
                    updated_at TIMESTAMPTZ DEFAULT NOW()
                );
                CREATE INDEX IF NOT EXISTS idx_jobs_status ON %s.scrape_jobs (status);
                CREATE INDEX IF NOT EXISTS
                    idx_jobs_idempotency_key ON %s.scrape_jobs (idempotency_key);
            $f$, safe_slug, safe_slug, safe_slug);

            -- scrape_results
            EXECUTE format($f$
                CREATE TABLE IF NOT EXISTS %s.scrape_results (
                    result_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    job_id UUID NOT NULL REFERENCES %s.scrape_jobs(job_id) ON DELETE CASCADE,
                    url TEXT NOT NULL,
                    success BOOLEAN NOT NULL,
                    http_status INTEGER,
                    is_challenge_page BOOLEAN NOT NULL DEFAULT FALSE,
                    level_used INTEGER NOT NULL,
                    proxy_used VARCHAR(45),
                    markdown TEXT,
                    json_data JSONB,
                    html_snapshot_url TEXT,
                    content_hash CHAR(64),
                    time_taken_ms INTEGER,
                    error_message TEXT,
                    failure_category VARCHAR(30),
                    extracted_at TIMESTAMPTZ DEFAULT NOW()
                );
                CREATE INDEX IF NOT EXISTS idx_results_job ON %s.scrape_results (job_id);
                CREATE INDEX IF NOT EXISTS
                    idx_results_url_hash ON %s.scrape_results (url, content_hash);
            $f$, safe_slug, safe_slug, safe_slug, safe_slug);

            -- dead_letter_queue (+ auto_retry_count, round 34). UNIQUE(job_id,
            -- url) lets proxy/dlq_reaper.py's auto-retry UPSERT the same row
            -- on a repeat failure (carrying auto_retry_count forward) instead
            -- of inserting a duplicate — a URL only ever has one live DLQ
            -- entry per job.
            EXECUTE format($f$
                CREATE TABLE IF NOT EXISTS %s.dead_letter_queue (
                    id SERIAL PRIMARY KEY,
                    job_id UUID NOT NULL,
                    url TEXT NOT NULL,
                    failure_category VARCHAR(50) NOT NULL,
                    error_message TEXT,
                    level_attempted INTEGER NOT NULL,
                    auto_retry_count INTEGER NOT NULL DEFAULT 0,
                    enqueued_at TIMESTAMPTZ DEFAULT NOW(),
                    dead_at TIMESTAMPTZ DEFAULT NOW(),
                    UNIQUE (job_id, url)
                );
            $f$, safe_slug);

            -- browser_sessions (round 42 — restored to migration 002's
            -- correct shape; 004/005/007 each silently reverted it back to
            -- 001's broken one via their own wholesale CREATE OR REPLACE
            -- FUNCTION. domain-keyed with expiry, matching what
            -- browser/session_state.py actually reads/writes.)
            EXECUTE format($f$
                CREATE TABLE IF NOT EXISTS %s.browser_sessions (
                    session_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    domain VARCHAR(255) NOT NULL,
                    storage_state JSONB NOT NULL,
                    last_used_at TIMESTAMPTZ DEFAULT NOW(),
                    expires_at TIMESTAMPTZ NOT NULL DEFAULT (NOW() + INTERVAL '30 days'),
                    UNIQUE (domain)
                );
                CREATE INDEX IF NOT EXISTS idx_sessions_expiry
                    ON %s.browser_sessions (expires_at);
            $f$, safe_slug, safe_slug);

            -- browser_profiles
            EXECUTE format($f$
                CREATE TABLE IF NOT EXISTS %s.browser_profiles (
                    profile_id VARCHAR(128) PRIMARY KEY,
                    storage_state_ref TEXT NOT NULL,
                    config_ref TEXT NOT NULL,
                    created_at TIMESTAMPTZ DEFAULT NOW(),
                    updated_at TIMESTAMPTZ DEFAULT NOW()
                );
            $f$, safe_slug);

            -- selector_history
            EXECUTE format($f$
                CREATE TABLE IF NOT EXISTS %s.selector_history (
                    id SERIAL PRIMARY KEY,
                    domain VARCHAR(255) NOT NULL,
                    target_key VARCHAR(100) NOT NULL,
                    selector_xpath TEXT,
                    selector_css TEXT,
                    version INTEGER DEFAULT 1,
                    success_count INTEGER DEFAULT 1,
                    failure_count INTEGER DEFAULT 0,
                    created_at TIMESTAMPTZ DEFAULT NOW(),
                    UNIQUE (domain, target_key, version)
                );
            $f$, safe_slug);

            -- webhook_outbox (round 34) — transactional outbox for both
            -- per-job notifications (job_id set) and operator-facing pool
            -- health alerts (job_id NULL, tenant_slug='system').
            EXECUTE format($f$
                CREATE TABLE IF NOT EXISTS %s.webhook_outbox (
                    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    job_id UUID,
                    event_type VARCHAR(40) NOT NULL,
                    payload JSONB NOT NULL,
                    target_url TEXT NOT NULL,
                    status VARCHAR(20) NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('pending', 'delivered', 'dead')),
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    delivered_at TIMESTAMPTZ
                );
                CREATE INDEX IF NOT EXISTS
                    idx_webhook_outbox_pending ON %s.webhook_outbox (status, next_attempt_at)
                    WHERE status = 'pending';
            $f$, safe_slug, safe_slug);
        END;
        $$ LANGUAGE plpgsql;
    """)

    # ── Fix every existing tenant schema's browser_sessions table in place.
    # See module docstring for why DROP + recreate is safe here (no schema
    # under the broken shape could hold real, readable session data). ──
    op.execute("""
        DO $$
        DECLARE
            tenant_schema text;
        BEGIN
            FOR tenant_schema IN
                SELECT nspname FROM pg_namespace
                WHERE nspname ~ '^[a-z][a-z0-9_]{2,62}$'
                  AND nspname NOT IN ('public', 'information_schema')
                  AND nspname NOT LIKE 'pg\\_%'
            LOOP
                EXECUTE format('DROP TABLE IF EXISTS %I.browser_sessions', tenant_schema);
                EXECUTE format(
                    'CREATE TABLE %I.browser_sessions (
                        session_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                        domain VARCHAR(255) NOT NULL,
                        storage_state JSONB NOT NULL,
                        last_used_at TIMESTAMPTZ DEFAULT NOW(),
                        expires_at TIMESTAMPTZ NOT NULL DEFAULT (NOW() + INTERVAL ''30 days''),
                        UNIQUE (domain)
                    )',
                    tenant_schema
                );
                EXECUTE format(
                    'CREATE INDEX IF NOT EXISTS idx_sessions_expiry
                        ON %I.browser_sessions (expires_at)',
                    tenant_schema
                );
            END LOOP;
        END;
        $$ LANGUAGE plpgsql;
    """)


def downgrade() -> None:
    # Restores 007's function body verbatim, browser_sessions regression and
    # all — this migration's whole point is to not re-introduce that bug, so
    # downgrade deliberately does NOT touch existing tenants' browser_sessions
    # tables (leaves the round-42-fixed shape in place rather than reverting
    # working tenants back to a known-broken one).
    op.execute("""
        CREATE OR REPLACE FUNCTION create_tenant_schema(tenant_slug text) RETURNS void AS $$
        DECLARE
            safe_slug text;
        BEGIN
            IF tenant_slug !~ '^[a-z][a-z0-9_]{2,62}$' THEN
                RAISE EXCEPTION 'invalid tenant_id: %', tenant_slug;
            END IF;
            safe_slug := quote_ident(tenant_slug);

            EXECUTE format('CREATE SCHEMA IF NOT EXISTS %s', safe_slug);

            EXECUTE format($f$
                CREATE TABLE IF NOT EXISTS %s.scrape_jobs (
                    job_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    urls TEXT[] NOT NULL,
                    config_used JSONB NOT NULL DEFAULT '{}',
                    status VARCHAR(20) NOT NULL DEFAULT 'PENDING'
                        CHECK (status IN (
                            'PENDING','PROCESSING','COMPLETED','FAILED','CANCELLED','DEAD_LETTER'
                        )),
                    webhook_url TEXT,
                    idempotency_key TEXT,
                    created_at TIMESTAMPTZ DEFAULT NOW(),
                    updated_at TIMESTAMPTZ DEFAULT NOW()
                );
                CREATE INDEX IF NOT EXISTS idx_jobs_status ON %s.scrape_jobs (status);
                CREATE INDEX IF NOT EXISTS
                    idx_jobs_idempotency_key ON %s.scrape_jobs (idempotency_key);
            $f$, safe_slug, safe_slug, safe_slug);

            EXECUTE format($f$
                CREATE TABLE IF NOT EXISTS %s.scrape_results (
                    result_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    job_id UUID NOT NULL REFERENCES %s.scrape_jobs(job_id) ON DELETE CASCADE,
                    url TEXT NOT NULL,
                    success BOOLEAN NOT NULL,
                    http_status INTEGER,
                    is_challenge_page BOOLEAN NOT NULL DEFAULT FALSE,
                    level_used INTEGER NOT NULL,
                    proxy_used VARCHAR(45),
                    markdown TEXT,
                    json_data JSONB,
                    html_snapshot_url TEXT,
                    content_hash CHAR(64),
                    time_taken_ms INTEGER,
                    error_message TEXT,
                    failure_category VARCHAR(30),
                    extracted_at TIMESTAMPTZ DEFAULT NOW()
                );
                CREATE INDEX IF NOT EXISTS idx_results_job ON %s.scrape_results (job_id);
                CREATE INDEX IF NOT EXISTS
                    idx_results_url_hash ON %s.scrape_results (url, content_hash);
            $f$, safe_slug, safe_slug, safe_slug, safe_slug);

            EXECUTE format($f$
                CREATE TABLE IF NOT EXISTS %s.dead_letter_queue (
                    id SERIAL PRIMARY KEY,
                    job_id UUID NOT NULL,
                    url TEXT NOT NULL,
                    failure_category VARCHAR(50) NOT NULL,
                    error_message TEXT,
                    level_attempted INTEGER NOT NULL,
                    auto_retry_count INTEGER NOT NULL DEFAULT 0,
                    enqueued_at TIMESTAMPTZ DEFAULT NOW(),
                    dead_at TIMESTAMPTZ DEFAULT NOW(),
                    UNIQUE (job_id, url)
                );
            $f$, safe_slug);

            EXECUTE format($f$
                CREATE TABLE IF NOT EXISTS %s.browser_sessions (
                    session_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    state JSONB NOT NULL,
                    created_at TIMESTAMPTZ DEFAULT NOW(),
                    updated_at TIMESTAMPTZ DEFAULT NOW()
                );
            $f$, safe_slug);

            EXECUTE format($f$
                CREATE TABLE IF NOT EXISTS %s.browser_profiles (
                    profile_id VARCHAR(128) PRIMARY KEY,
                    storage_state_ref TEXT NOT NULL,
                    config_ref TEXT NOT NULL,
                    created_at TIMESTAMPTZ DEFAULT NOW(),
                    updated_at TIMESTAMPTZ DEFAULT NOW()
                );
            $f$, safe_slug);

            EXECUTE format($f$
                CREATE TABLE IF NOT EXISTS %s.selector_history (
                    id SERIAL PRIMARY KEY,
                    domain VARCHAR(255) NOT NULL,
                    target_key VARCHAR(100) NOT NULL,
                    selector_xpath TEXT,
                    selector_css TEXT,
                    version INTEGER DEFAULT 1,
                    success_count INTEGER DEFAULT 1,
                    failure_count INTEGER DEFAULT 0,
                    created_at TIMESTAMPTZ DEFAULT NOW(),
                    UNIQUE (domain, target_key, version)
                );
            $f$, safe_slug);

            EXECUTE format($f$
                CREATE TABLE IF NOT EXISTS %s.webhook_outbox (
                    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    job_id UUID,
                    event_type VARCHAR(40) NOT NULL,
                    payload JSONB NOT NULL,
                    target_url TEXT NOT NULL,
                    status VARCHAR(20) NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('pending', 'delivered', 'dead')),
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    delivered_at TIMESTAMPTZ
                );
                CREATE INDEX IF NOT EXISTS
                    idx_webhook_outbox_pending ON %s.webhook_outbox (status, next_attempt_at)
                    WHERE status = 'pending';
            $f$, safe_slug, safe_slug);
        END;
        $$ LANGUAGE plpgsql;
    """)
