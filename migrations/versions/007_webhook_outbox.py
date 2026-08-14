# migrations/versions/007_webhook_outbox.py
"""Add webhook_outbox table and dead_letter_queue.auto_retry_count.

Round 34: webhook delivery was fire-and-forget (one inline POST attempt, log
and drop on failure) — a crash or a rejected delivery left no trace anywhere
a caller could see. webhook_outbox is a transactional-outbox table (same
per-tenant-schema shape as dead_letter_queue) that orchestrator/tasks.py
writes to before attempting delivery, and orchestrator/webhook_sweeper.py
polls for anything still pending/retryable — so a lost notification is now a
durable, queryable fact instead of a line in a dead worker process's stdout.

dead_letter_queue.auto_retry_count supports the companion fix: PROXY_EXHAUSTED
and CIRCUIT_OPEN entries are transient (they resolve once the pool/circuit
recovers) but were previously binned with permanent failures and never
retried automatically. proxy/dlq_reaper.py re-enqueues eligible entries and
needs a bounded counter to stop after a configured cap rather than looping a
flapping job forever.

Additive, per-tenant, following the same create_tenant_schema function-based
pattern as 002/003/004/005.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "007"
down_revision: str | None = "006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ── Update create_tenant_schema() so new tenants get both from the start ──
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
                        ADD COLUMN IF NOT EXISTS auto_retry_count INTEGER NOT NULL DEFAULT 0',
                    tenant_schema
                );
                -- Best-effort: a pre-existing schema with duplicate (job_id, url)
                -- rows would violate this constraint — auto-retry UPSERT degrades
                -- gracefully to plain INSERT there (see storage/dlq.py), it isn't
                -- load-bearing enough to abort the whole migration over.
                BEGIN
                    IF to_regclass(format('%I.dead_letter_queue', tenant_schema)) IS NOT NULL THEN
                        EXECUTE format(
                            'ALTER TABLE %I.dead_letter_queue
                                ADD CONSTRAINT dead_letter_queue_job_id_url_key
                                UNIQUE (job_id, url)',
                            tenant_schema
                        );
                    END IF;
                EXCEPTION WHEN OTHERS THEN
                    RAISE NOTICE 'skipping dead_letter_queue unique constraint for %: %',
                        tenant_schema, SQLERRM;
                END;
                EXECUTE format(
                    'CREATE TABLE IF NOT EXISTS %I.webhook_outbox (
                        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                        job_id UUID,
                        event_type VARCHAR(40) NOT NULL,
                        payload JSONB NOT NULL,
                        target_url TEXT NOT NULL,
                        status VARCHAR(20) NOT NULL DEFAULT ''pending''
                            CHECK (status IN (''pending'', ''delivered'', ''dead'')),
                        attempts INTEGER NOT NULL DEFAULT 0,
                        next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        delivered_at TIMESTAMPTZ
                    )',
                    tenant_schema
                );
                IF to_regclass(format('%I.webhook_outbox', tenant_schema)) IS NOT NULL THEN
                    EXECUTE format(
                        'CREATE INDEX IF NOT EXISTS idx_webhook_outbox_pending
                            ON %I.webhook_outbox (status, next_attempt_at)
                            WHERE status = ''pending''',
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
                EXECUTE format('DROP TABLE IF EXISTS %I.webhook_outbox', tenant_schema);
                EXECUTE format(
                    'ALTER TABLE IF EXISTS %I.dead_letter_queue
                        DROP CONSTRAINT IF EXISTS dead_letter_queue_job_id_url_key,
                        DROP COLUMN IF EXISTS auto_retry_count',
                    tenant_schema
                );
            END LOOP;
        END;
        $$ LANGUAGE plpgsql;
    """)
