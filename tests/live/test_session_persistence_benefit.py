# tests/live/test_session_persistence_benefit.py
"""Live functional verification of browser_sessions — closes .wolf/STATUS.md's
open item: "worth live-verifying persisted session state actually improves
anything measurable" (round 42 made the plumbing itself functional;
tests/live/test_session_persistence.py already proves the DB round-trip is
structurally correct, but nothing had verified a real, measurable benefit).

Uses the local challenge-mirror fixture (tests/fixtures/challenge_mirror,
BD-05) at `strict` difficulty — the tier that specifically targets L3
(Camoufox-only) and enforces a real 3-second minimum PoW-solve delay — as a
legally-clean, deterministic, owned stand-in for a real anti-bot product,
rather than pointing this at a real commercial site (flaky, and touches
infrastructure this project doesn't own).

Mechanism under test: a domain's `challenge_pass` cookie, once earned by
solving the challenge, is persisted to Postgres (browser/session_state.py)
and replayed into the *next* cold Camoufox launch for that domain — this
specifically matters across jobs/process restarts, since rq forks a fresh
worker process per job (orchestrator/tasks.py) and no in-memory pool
survives between them. This test simulates that with the same
force-eviction-then-reacquire technique test_session_persistence.py already
uses (cookie survival through Postgres is identical whether the reload
happens because of pool eviction or a brand-new process — session_mgr.load()
can't tell the difference).

Requires: real Camoufox + real Postgres, matching
tests/live/test_session_persistence.py's requirements, plus the
challenge-mirror fixture server (started here as a subprocess on :8090, per
CLAUDE.md's documented `python -m app.server` invocation).
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
import time
import uuid
from pathlib import Path

import httpx
import pytest

_MIRROR_DIR = Path(__file__).parent.parent / "fixtures" / "challenge_mirror"
_MIRROR_PORT = 8090
_MIRROR_URL = f"http://127.0.0.1:{_MIRROR_PORT}"


@pytest.fixture
def challenge_mirror():
    proc = subprocess.Popen(
        [sys.executable, "-m", "app.server"],
        cwd=str(_MIRROR_DIR),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                resp = httpx.get(f"{_MIRROR_URL}/health", timeout=1)
                if resp.status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.2)
        else:
            proc.terminate()
            raise RuntimeError("challenge-mirror fixture never became healthy on :8090")
        yield _MIRROR_URL
    finally:
        proc.terminate()
        proc.wait(timeout=5)


async def _wait_for_verified_content(page, timeout_s: float = 30.0) -> float:
    """Poll page content for the challenge-mirror's success marker. Returns
    elapsed wall-clock seconds. A cookie-primed visit should satisfy this on
    the very first check (no challenge ever issued); a cold visit must first
    sit through the server-enforced 3s minimum solve delay for the `strict`
    tier, plus real PoW compute time, plus a POST /verify + redirect."""
    start = time.monotonic()
    deadline = start + timeout_s
    while time.monotonic() < deadline:
        try:
            content = await page.content()
        except Exception:
            # The challenge page's own JS does a same-tab redirect
            # (window.location.href = '/') on success — page.content() can
            # legitimately race that navigation mid-poll. Not a real
            # failure, just try again next tick.
            await asyncio.sleep(0.2)
            continue
        if "Verified Content" in content:
            return time.monotonic() - start
        await asyncio.sleep(0.2)
    raise AssertionError("challenge-mirror never returned Verified Content within timeout")


@pytest.mark.live
@pytest.mark.asyncio
async def test_persisted_session_skips_challenge_on_next_cold_launch(challenge_mirror) -> None:
    unique_id = uuid.uuid4().hex[:12]
    domain = f"{unique_id}.challenge-mirror.local"
    challenge_url = f"{challenge_mirror}/?difficulty=strict"

    from scraper_engine.browser.pool import BrowserPool
    from scraper_engine.browser.session_state import SessionStateManager
    from scraper_engine.core.tenant import TenantId
    from scraper_engine.storage.postgres_client import PostgresClient

    tenant = TenantId("system")

    pg = PostgresClient(
        pgbouncer_dsn="postgresql://scraper:scraper@localhost:5432/scraper_engine",
        pool_size=5,
    )
    await pg.start()
    await pg.execute(
        tenant,
        """
        CREATE TABLE IF NOT EXISTS browser_sessions (
            session_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            domain VARCHAR(255) NOT NULL,
            storage_state JSONB NOT NULL,
            last_used_at TIMESTAMPTZ DEFAULT NOW(),
            expires_at TIMESTAMPTZ NOT NULL DEFAULT (NOW() + INTERVAL '30 days'),
            UNIQUE (domain)
        )
        """,
    )

    session_mgr = SessionStateManager(pg=pg, ttl_days=30)
    pool = BrowserPool(
        tenant_id=tenant, prewarm_count=1, max_idle_seconds=300, session_mgr=session_mgr
    )
    await pool.start()

    try:
        # ── Phase 1: cold visit, no prior session — must solve the challenge ──
        async with pool.lease(domain=domain) as ctx:
            page = await ctx.new_page()
            await page.goto(challenge_url)
            cold_elapsed = await _wait_for_verified_content(page)
            print(f"PHASE 1 (cold, no session) — solved in {cold_elapsed:.2f}s")
            cookies = await ctx.cookies()
            assert any(c["name"] == "challenge_pass" for c in cookies), (
                "phase 1 should have earned a real challenge_pass cookie by solving"
            )
            await page.close()
        # pool.lease's healthy exit just persisted this cookie to Postgres.

        # ── Phase 2: force the warm context out, simulating a new job/process ──
        async with pool.lease(domain="different-domain.invalid") as _:
            pass

        # ── Phase 3: cold re-launch for the same domain — session_mgr.load()
        # must replay the persisted challenge_pass cookie before the first
        # navigation even happens. ──
        async with pool.lease(domain=domain) as ctx2:
            page2 = await ctx2.new_page()
            await page2.goto(challenge_url)
            primed_elapsed = await _wait_for_verified_content(page2)
            print(f"PHASE 3 (cold, session-primed) — resolved in {primed_elapsed:.2f}s")
            first_content = await page2.content()
            assert "Verifying your browser" not in first_content, (
                "a session-primed cold launch must skip the challenge page entirely, "
                "not just eventually pass it"
            )
            await page2.close()

        print(
            f"RESULT — cold: {cold_elapsed:.2f}s vs session-primed: {primed_elapsed:.2f}s "
            f"({cold_elapsed - primed_elapsed:.2f}s saved, challenge skipped entirely)"
        )
        # The `strict` tier alone enforces a 3s minimum solve delay server-side
        # (DIFFICULTY_CONFIG["strict"]["min_solve_seconds"]) before real PoW
        # compute and a network round trip on top — a session-primed run has
        # none of that, so this margin is conservative, not a coin-flip.
        assert primed_elapsed < cold_elapsed - 1.0, (
            f"expected a measurable speedup from skipping the challenge entirely, "
            f"got cold={cold_elapsed:.2f}s primed={primed_elapsed:.2f}s"
        )
    finally:
        await pool.shutdown()
        await session_mgr.delete(tenant, domain)
        await pg.stop()
