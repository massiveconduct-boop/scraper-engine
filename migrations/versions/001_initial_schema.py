# migrations/versions/001_initial_schema.py
"""Initial schema — global tables + per-tenant schema bootstrap.

Spec §5 DDL: proxy_pool, domain_ban_history, api_keys, tenants,
plus per-tenant: scrape_jobs, scrape_results, dead_letter_queue,
browser_sessions, browser_profiles, selector_history.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ===== GLOBAL (public schema; NOT tenant-scoped) =====

    op.create_table(
        "proxy_pool",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("ip", sa.String(45), nullable=False),
        sa.Column("port", sa.Integer(), nullable=False),
        sa.Column(
            "protocol",
            sa.String(10),
            nullable=False,
            comment="HTTP|HTTPS|SOCKS4|SOCKS5",
        ),
        sa.Column(
            "anonymity_level",
            sa.String(20),
            nullable=False,
            server_default="transparent",
            comment="transparent|anonymous|elite",
        ),
        sa.Column(
            "asn_class",
            sa.String(20),
            nullable=False,
            server_default="unknown",
            comment="datacenter|residential|mobile|unknown",
        ),
        sa.Column("response_time_ms", sa.Integer()),
        sa.Column(
            "reliability_score",
            sa.Float(),
            nullable=False,
            server_default="50.0",
        ),
        sa.Column(
            "global_failure_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "last_validated",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
        ),
        sa.Column("last_used", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("ip", "port", "protocol"),
    )
    op.create_index(
        "idx_proxy_score", "proxy_pool", ["reliability_score"], postgresql_using="btree"
    )

    op.create_table(
        "domain_ban_history",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("ip", sa.String(45), nullable=False),
        sa.Column("domain", sa.String(255), nullable=False),
        sa.Column("banned_until", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reason", sa.String(50), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "idx_ban_ip_domain",
        "domain_ban_history",
        ["ip", "domain", "banned_until"],
    )

    op.create_table(
        "api_keys",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("api_key", sa.String(128), nullable=False, unique=True),
        sa.Column(
            "tenant_slug",
            sa.String(64),
            nullable=False,
            comment="Validated by TenantId: ^[a-z][a-z0-9_]{2,62}$",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
        ),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
    )

    op.create_table(
        "tenants",
        sa.Column(
            "tenant_id",
            sa.String(64),
            primary_key=True,
            comment="Validated: ^[a-z][a-z0-9_]{2,62}$",
        ),
        sa.Column(
            "quota_daily_limit",
            sa.Integer(),
            nullable=False,
            server_default="10000",
        ),
        sa.Column(
            "capsolver_daily_credit_ceiling",
            sa.Float(),
            nullable=False,
            server_default="1.0",
            comment="BD-03: $1.00/day",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
        ),
    )

    # Per-tenant schema bootstrap function
    op.execute("""
        CREATE OR REPLACE FUNCTION create_tenant_schema(tenant_slug text) RETURNS void AS $$
        DECLARE
            safe_slug text;
        BEGIN
            -- Defense in depth: re-validate before DDL construction (invariant §1.1.7)
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
                    extracted_at TIMESTAMPTZ DEFAULT NOW()
                );
                CREATE INDEX IF NOT EXISTS idx_results_job ON %s.scrape_results (job_id);
                CREATE INDEX IF NOT EXISTS
                    idx_results_url_hash ON %s.scrape_results (url, content_hash);
            $f$, safe_slug, safe_slug, safe_slug);

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

    # Bootstrap the 'system' tenant schema
    op.execute("SELECT create_tenant_schema('system')")


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS create_tenant_schema(text) CASCADE")
    op.drop_table("tenants")
    op.drop_table("api_keys")
    op.drop_index("idx_ban_ip_domain", table_name="domain_ban_history")
    op.drop_table("domain_ban_history")
    op.drop_index("idx_proxy_score", table_name="proxy_pool")
    op.drop_table("proxy_pool")
