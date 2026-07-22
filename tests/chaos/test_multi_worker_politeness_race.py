"""
Closes G-06 from the production-readiness gap audit.

Multi-worker politeness race test. Verifies that N concurrent workers
(whether asyncio tasks simulating multiple processes) cannot exceed the
configured max_concurrent slots for a single domain.

The Lua-script atomicity fix from F-06 is validated here under actual
concurrency, not just in isolation.
"""

import asyncio

import pytest

from core.tenant import TenantId


@pytest.fixture
async def redis():
    from redis.asyncio import Redis
    r = Redis(host="localhost", port=6379, decode_responses=True)
    yield r
    await r.aclose()


class TestPolitenessRace:
    """G-06: politeness holds across concurrent workers."""

    @pytest.mark.asyncio
    async def test_slots_never_exceed_max_concurrent(self, redis):
        """10 concurrent tasks (simulating 10 worker processes) racing for 2 slots.

        At every sampled instant, SCARD must never exceed 2.
        """
        from orchestrator.politeness import ACQUIRE_SLOT_LUA, RELEASE_SLOT_LUA

        domain = "racetest.internal"
        tenant = TenantId("g06test")
        slot_key = f"politeness:slots:{tenant}:{domain}"
        max_concurrent = 2
        worker_id_prefix = "worker-"

        # Clean up before test
        await redis.delete(slot_key)

        max_observed = 0

        async def worker_task(worker_index):
            nonlocal max_observed
            worker_id = f"{worker_id_prefix}{worker_index}"
            for _ in range(5):
                # Try to acquire
                result = await redis.eval(
                    ACQUIRE_SLOT_LUA, 1,
                    slot_key, worker_id,
                    max_concurrent, 300,
                )
                if result == 1:
                    await asyncio.sleep(0.01)
                    card = await redis.scard(slot_key)
                    if card > max_observed:
                        max_observed = card
                    await redis.eval(
                        RELEASE_SLOT_LUA, 1, slot_key, worker_id
                    )
                await asyncio.sleep(0.005)

        await asyncio.gather(*[worker_task(i) for i in range(10)])

        assert max_observed <= max_concurrent, (
            f"POLITENESS RACE: {max_observed} concurrent > {max_concurrent} max"
        )
        print(f"  max observed: {max_observed}, max allowed: {max_concurrent}")

        # Clean up
        await redis.delete(slot_key)
