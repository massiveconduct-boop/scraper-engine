# migrations/versions/012_dlq_retry_not_before.py
"""Add dead_letter_queue.retry_not_before.

Round 70 — a terminal HTTP 429 gets its own failure_category, rate_limited,
which proxy/dlq_reaper.py re-drives after a wait. When the site sent a
Retry-After header, the wait must honour it (capped at
fetcher/_failure.py::RETRY_AFTER_CAP_SECONDS), and nothing in the DLQ row
could hold it: dead_at and enqueued_at are the only timestamps. NULL means
"no site-requested wait" — every existing row, and every other category.

Additive column only, following the same create_tenant_schema
function-based pattern as 004-011: redefine the function so new tenants get
the column from the start, then backfill every existing tenant schema in
place.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "012"
down_revision: str | None = "011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ── Redefine create_tenant_schema() with retry_not_before on
    # dead_letter_queue — identical to 011's definition otherwise. ──
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

            -- scrape_jobs (+ started_at/finished_at, round 63)
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
                    updated_at TIMESTAMPTZ DEFAULT NOW(),
                    started_at TIMESTAMPTZ,
                    finished_at TIMESTAMPTZ
                );
                CREATE INDEX IF NOT EXISTS idx_jobs_status ON %s.scrape_jobs (status);
                CREATE INDEX IF NOT EXISTS
                    idx_jobs_idempotency_key ON %s.scrape_jobs (idempotency_key);
            $f$, safe_slug, safe_slug, safe_slug);

            -- scrape_results (+ timings, round 63)
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
                    proxy_source VARCHAR(20),
                    markdown TEXT,
                    json_data JSONB,
                    network_events JSONB,
                    timings JSONB,
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

            -- dead_letter_queue
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
                    retry_not_before TIMESTAMPTZ,
                    UNIQUE (job_id, url)
                );
            $f$, safe_slug);

            -- browser_sessions
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

            -- webhook_outbox
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

    # ── Backfill every existing tenant schema in place ──
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
                EXECUTE format(
                    'ALTER TABLE IF EXISTS %I.dead_letter_queue
                        ADD COLUMN IF NOT EXISTS retry_not_before TIMESTAMPTZ',
                    tenant_schema
                );
            END LOOP;
        END;
        $$ LANGUAGE plpgsql;
    """)


def downgrade() -> None:
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
                EXECUTE format(
                    'ALTER TABLE IF EXISTS %I.dead_letter_queue
                        DROP COLUMN IF EXISTS retry_not_before',
                    tenant_schema
                );
            END LOOP;
        END;
        $$ LANGUAGE plpgsql;
    """)

    # Restore 011's definition verbatim.
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

            -- scrape_jobs (+ started_at/finished_at, round 63)
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
                    updated_at TIMESTAMPTZ DEFAULT NOW(),
                    started_at TIMESTAMPTZ,
                    finished_at TIMESTAMPTZ
                );
                CREATE INDEX IF NOT EXISTS idx_jobs_status ON %s.scrape_jobs (status);
                CREATE INDEX IF NOT EXISTS
                    idx_jobs_idempotency_key ON %s.scrape_jobs (idempotency_key);
            $f$, safe_slug, safe_slug, safe_slug);

            -- scrape_results (+ timings, round 63)
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
                    proxy_source VARCHAR(20),
                    markdown TEXT,
                    json_data JSONB,
                    network_events JSONB,
                    timings JSONB,
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

            -- dead_letter_queue
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

            -- browser_sessions
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

            -- webhook_outbox
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
