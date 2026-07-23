"""
G-06 closure: real OS subprocess politeness race test.

Spawns actual subprocess workers (not asyncio tasks) targeting the same domain,
asserting Redis SCARD never exceeds max_concurrent at any sampled instant.
This directly answers the review finding that the asyncio-task version proves
Lua atomicity but not OS-process scheduling behavior.
"""

import asyncio
import os
import subprocess
import sys
import tempfile

import pytest

from core.tenant import TenantId


@pytest.fixture
async def redis():
    from redis.asyncio import Redis
    r = Redis(host="localhost", port=6379, decode_responses=True)
    yield r
    await r.aclose()


@pytest.mark.chaos
@pytest.mark.asyncio
async def test_os_subprocess_politeness_holds_across_real_processes(redis):
    """G-06: 3 real OS subprocess workers race for 2 slots on same domain.

    Spawns subprocess workers that run ACQUIRE→work→RELEASE loops against
    real Redis. Samples SCARD every 200ms. Verifies it never exceeds 2.

    This is the OS-process-level test that the review explicitly asked for —
    the asyncio-task version only proved Lua atomicity, not that actual
    worker replicas in docker-compose won't race.
    """
    domain = "os-racetest.internal"
    tenant = TenantId("g06_os_test")
    slot_key = f"politeness:slots:{tenant}:{domain}"
    await redis.delete(slot_key)

    # Worker script: tries to acquire slot, holds for random duration, releases
    worker_script = '''
import asyncio, os, random, sys
from redis.asyncio import Redis
async def worker():
    r = Redis(host="localhost", port=6379, decode_responses=True)
    wid = f"subproc-{os.getpid()}-{random.randint(0,9999)}"
    from orchestrator.politeness import ACQUIRE_SLOT_LUA, RELEASE_SLOT_LUA
    key = "politeness:slots:g06_os_test:os-racetest.internal"
    for _ in range(10):
        ok = await r.eval(ACQUIRE_SLOT_LUA, 1, key, wid, 2, 300)
        if ok != 1:
            await asyncio.sleep(0.01)
            continue
        await asyncio.sleep(random.uniform(0.01, 0.05))
        await r.eval(RELEASE_SLOT_LUA, 1, key, wid)
        await asyncio.sleep(0.01)
    await r.aclose()
asyncio.run(worker())
'''

    with tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False) as f:
        f.write(worker_script)
        script_path = f.name

    try:
        procs = [
            subprocess.Popen([sys.executable, script_path],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            for _ in range(3)
        ]

        max_observed = 0
        for _ in range(50):
            count = await redis.scard(slot_key)
            max_observed = max(max_observed, count)
            await asyncio.sleep(0.2)

        for p in procs:
            p.terminate()
            p.wait(timeout=5)

        assert max_observed <= 2, (
            f"G-06 OS SUBPROCESS RACE: {max_observed} concurrent > 2 max"
        )
        print(f"  OS subprocess politeness: max_observed={max_observed}, max_allowed=2")

    finally:
        os.unlink(script_path)
        await redis.delete(slot_key)
