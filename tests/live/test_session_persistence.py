# tests/live/test_session_persistence.py
"""Live session persistence test — requires Camoufox + Postgres running.

Per plan §5.5 and round-8 directive Item C: proves session state survives
a full pool recycle by printing actual cookie values at each phase,
not just asserting. A human-visible matching value pair is what
actually proves persistence.
"""

import uuid

import pytest


@pytest.mark.live
@pytest.mark.asyncio
async def test_session_survives_pool_recycle():
    """Acquire for a unique domain, set a distinctive cookie, release healthy.
    Force the warm context out of the pool, then re-acquire the same domain.
    Print the cookie value at STEP 1 and STEP 2 — matching values prove
    the session was loaded from the database, not from a surviving warm context.
    """
    unique_id = uuid.uuid4().hex[:12]
    domain = f"{unique_id}.example.com"
    cookie_name = "session_persistence_probe"
    cookie_value = f"probe-{unique_id}"

    from browser.pool import BrowserPool
    from browser.session_state import SessionStateManager
    from core.tenant import TenantId
    from storage.postgres_client import PostgresClient

    tenant = TenantId("system")

    pg = PostgresClient(
        pgbouncer_dsn="postgresql://scraper:scraper@localhost:5432/scraper_engine",
        pool_size=5,
    )
    await pg.start()

    await pg.execute(tenant, """
        CREATE TABLE IF NOT EXISTS browser_sessions (
            session_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            domain VARCHAR(255) NOT NULL,
            storage_state JSONB NOT NULL,
            last_used_at TIMESTAMPTZ DEFAULT NOW(),
            expires_at TIMESTAMPTZ NOT NULL DEFAULT (NOW() + INTERVAL '30 days'),
            UNIQUE (domain)
        )
    """)

    session_mgr = SessionStateManager(pg=pg, ttl_days=30)

    pool = BrowserPool(
        tenant_id=tenant,
        prewarm_count=1,
        max_idle_seconds=300,
        session_mgr=session_mgr,
    )
    await pool.start()

    try:
        # ── Phase 1: acquire, set cookie, release healthy ──
        async with pool.lease(domain=domain) as ctx:
            await ctx.add_cookies([
                {
                    "name": cookie_name,
                    "value": cookie_value,
                    "domain": ".example.com",
                    "path": "/",
                }
            ])
            state = await ctx.storage_state()
            cookie_written = next(
                (c for c in state["cookies"] if c["name"] == cookie_name), None
            )
            print(f"STEP 1 - cookie written to live context: {cookie_written}")
            assert cookie_written is not None and cookie_written["value"] == cookie_value

        # ── Phase 2: force eviction ──
        # Acquire with a different domain triggers domain-mismatch teardown
        # in acquire(), forcing the warm context out of the pool.
        async with pool.lease(domain="different-domain.invalid") as _:
            pass

        # ── Phase 3: re-acquire original domain ──
        # The warm context was torn down, so the pool must launch fresh.
        # SessionStateManager must load the persisted state from Postgres.
        async with pool.lease(domain=domain) as ctx2:
            cookies = await ctx2.cookies()
            reloaded = next(
                (c for c in cookies if c["name"] == cookie_name), None
            )
            print(f"STEP 2 - cookie reloaded from persisted session: {reloaded}")
            assert reloaded is not None, "session did not persist across pool recycle"
            assert reloaded["value"] == cookie_value, (
                f"cookie value mismatch: expected {cookie_value!r}, got {reloaded['value']!r}"
            )

        print("STEP 3 - PASS: cookie value round-tripped through Postgres, not memory")

    finally:
        await pool.shutdown()
        await session_mgr.delete(tenant, domain)
        await pg.stop()
