# migrations/versions/004_result_error_columns.py
"""Add error_message/failure_category to scrape_results.

Wiring the execution pipeline (API -> queue -> Worker -> persist) means every
FetchResult can now be written back to scrape_results, but FailureCategory/
error_message previously had nowhere to land on that table (they only ever
reached dead_letter_queue, which is populated for a narrower set of terminal
failures). Additive columns, per-tenant, following the same
create_tenant_schema function-based pattern as 001/002/003.

Updates create_tenant_schema() so new tenants get the columns from the start,
and backfills every existing tenant schema in place.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "004"
down_revision: str | None = "003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ── Update create_tenant_schema() so new tenants get the columns ──
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

            -- scrape_jobs
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
                    created_at TIMESTAMPTZ DEFAULT NOW(),
                    updated_at TIMESTAMPTZ DEFAULT NOW()
                );
                CREATE INDEX IF NOT EXISTS idx_jobs_status ON %s.scrape_jobs (status);
            $f$, safe_slug, safe_slug);

            -- scrape_results (+ error_message/failure_category, round 22)
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

            -- dead_letter_queue
            EXECUTE format($f$
                CREATE TABLE IF NOT EXISTS %s.dead_letter_queue (
                    id SERIAL PRIMARY KEY,
                    job_id UUID NOT NULL,
                    url TEXT NOT NULL,
                    failure_category VARCHAR(50) NOT NULL,
                    error_message TEXT,
                    level_attempted INTEGER NOT NULL,
                    enqueued_at TIMESTAMPTZ DEFAULT NOW(),
                    dead_at TIMESTAMPTZ DEFAULT NOW()
                );
            $f$, safe_slug);

            -- browser_sessions
            EXECUTE format($f$
                CREATE TABLE IF NOT EXISTS %s.browser_sessions (
                    session_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    state JSONB NOT NULL,
                    created_at TIMESTAMPTZ DEFAULT NOW(),
                    updated_at TIMESTAMPTZ DEFAULT NOW()
                );
            $f$, safe_slug);

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
            LOOP
                EXECUTE format(
                    'ALTER TABLE IF EXISTS %I.scrape_results
                        ADD COLUMN IF NOT EXISTS error_message TEXT,
                        ADD COLUMN IF NOT EXISTS failure_category VARCHAR(30)',
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
            LOOP
                EXECUTE format(
                    'ALTER TABLE IF EXISTS %I.scrape_results
                        DROP COLUMN IF EXISTS error_message,
                        DROP COLUMN IF EXISTS failure_category',
                    tenant_schema
                );
            END LOOP;
        END;
        $$ LANGUAGE plpgsql;
    """)
