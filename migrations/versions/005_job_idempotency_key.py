# migrations/versions/005_job_idempotency_key.py
"""Add idempotency_key to scrape_jobs.

Lets a caller tag POST /v1/scrape and /v1/crawl with a repeat-safe key
(Idempotency-Key header). A retry with the same key while the original job
is still live returns the original job instead of enqueuing a duplicate and
double-charging quota. Non-unique index only, dedup semantics (excluding
dead terminal states) live in the query, not a DB constraint. Additive
column, per-tenant, following the same create_tenant_schema function-based
pattern as 002/003/004.

Updates create_tenant_schema() so new tenants get the column from the start,
and backfills every existing tenant schema in place.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "005"
down_revision: str | None = "004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ── Update create_tenant_schema() so new tenants get the column ──
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
                  AND nspname NOT LIKE 'pg\\_%'
            LOOP
                EXECUTE format(
                    'ALTER TABLE IF EXISTS %I.scrape_jobs
                        ADD COLUMN IF NOT EXISTS idempotency_key TEXT',
                    tenant_schema
                );
                IF to_regclass(format('%I.scrape_jobs', tenant_schema)) IS NOT NULL THEN
                    EXECUTE format(
                        'CREATE INDEX IF NOT EXISTS idx_jobs_idempotency_key
                            ON %I.scrape_jobs (idempotency_key)',
                        tenant_schema
                    );
                END IF;
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
                    'DROP INDEX IF EXISTS %I.idx_jobs_idempotency_key',
                    tenant_schema
                );
                EXECUTE format(
                    'ALTER TABLE IF EXISTS %I.scrape_jobs
                        DROP COLUMN IF EXISTS idempotency_key',
                    tenant_schema
                );
            END LOOP;
        END;
        $$ LANGUAGE plpgsql;
    """)
