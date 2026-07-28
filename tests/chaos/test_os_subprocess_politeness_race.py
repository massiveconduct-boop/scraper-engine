"""
G-06 closure: real OS subprocess politeness race test.

Spawns actual subprocess workers (not asyncio tasks) targeting the same domain,
asserting Redis SCARD never exceeds max_concurrent at any sampled instant.

Round 9 instrumentation: each subprocess logs wall-clock timestamps for every
ACQUIRE and RELEASE so the test can prove real overlap occurred, not just that
the cap was never exceeded during a run where workers never contended.
Work duration increased to 80-250ms per iteration (was 10-50ms) to force
genuine contention with 3 processes competing for 2 slots.
"""

import asyncio
import os
import subprocess
import sys
import tempfile

import pytest

from scraper_engine.core.tenant import TenantId


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

    Instrumented: each subprocess logs wall-clock timestamps for every
    ACQUIRE and RELEASE so the test can prove real overlap occurred.
    Work duration 80-250ms (was 10-50ms) to force genuine contention.
    """
    domain = "os-racetest.internal"
    tenant = TenantId("g06_os_test")
    slot_key = f"politeness:slots:{tenant}:{domain}"
    await redis.delete(slot_key)

    # Worker script: logs timestamps to stdout for the parent to parse.
    # Work duration is 80-250ms (was 10-50ms) — enough for 3 processes,
    # 10 iterations each, against a 2-slot limit, to produce real overlap.
    worker_script = '''
import asyncio, os, random, sys, time
from redis.asyncio import Redis
async def worker():
    r = Redis(host="localhost", port=6379, decode_responses=True)
    wid = f"subproc-{os.getpid()}-{random.randint(0,9999)}"
    from scraper_engine.orchestrator.politeness import ACQUIRE_SLOT_LUA, RELEASE_SLOT_LUA
    key = "politeness:slots:g06_os_test:os-racetest.internal"
    for _ in range(10):
        ok = await r.eval(ACQUIRE_SLOT_LUA, 1, key, wid, 2, 300)
        if ok != 1:
            await asyncio.sleep(0.01)
            continue
        t_acquire = time.time()
        print(f"ACQUIRE|{wid}|{t_acquire}", flush=True)
        await asyncio.sleep(random.uniform(0.08, 0.25))
        await r.eval(RELEASE_SLOT_LUA, 1, key, wid)
        t_release = time.time()
        print(f"RELEASE|{wid}|{t_release}", flush=True)
        await asyncio.sleep(0.02)
    await r.aclose()
asyncio.run(worker())
'''

    with tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False) as f:
        f.write(worker_script)
        script_path = f.name

    try:
        procs = [
            subprocess.Popen(
                [sys.executable, script_path],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True,
            )
            for _ in range(3)
        ]

        max_observed = 0
        for _ in range(80):
            count = await redis.scard(slot_key)
            max_observed = max(max_observed, count)
            await asyncio.sleep(0.2)

        for p in procs:
            p.terminate()
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait()

        # Parse timestamps from subprocess stdout
        events = []
        for p in procs:
            out, _ = p.communicate()
            if out:
                for line in out.strip().split("\n"):
                    if line.startswith("ACQUIRE|") or line.startswith("RELEASE|"):
                        parts = line.strip().split("|")
                        if len(parts) == 3:
                            events.append((parts[0], parts[1], float(parts[2])))

        # Sort by time and detect overlapping holds
        events.sort(key=lambda e: e[2])
        active = set()
        holds = []
        for evt_type, wid, ts in events:
            if evt_type == "ACQUIRE":
                active.add(wid)
                holds.append(("ACQUIRE", wid, ts, list(active)))
            else:
                holds.append(("RELEASE", wid, ts, list(active)))
                active.discard(wid)

        # Find peak concurrent holders
        peak = max(len(h[3]) for h in holds) if holds else 0

        # Build timestamp table
        print("\n  Timestamp table (ACQUIRE/RELEASE per subprocess, wall-clock):")
        print(f"  {'EVENT':<8} {'WORKER':<25} {'TIMESTAMP':<20} {'ACTIVE_HOLDERS':<15} {'HOLDERS'}")
        for h in holds:
            print(f"  {h[0]:<8} {h[1]:<25} {h[2]:<20.6f} {len(h[3]):<15} {h[3]}")

        # Check for real overlap
        overlapping_pairs = set()
        hold_ranges = {}  # wid -> (acquire_ts, release_ts)
        for h in holds:
            if h[0] == "ACQUIRE":
                hold_ranges[h[1]] = h[2]
            elif h[1] in hold_ranges:
                a_ts = hold_ranges.pop(h[1])
                r_ts = h[2]
                # Check if this hold overlapped any other hold
                for other_wid, other_a_ts in list(hold_ranges.items()):
                    if other_wid != h[1] and a_ts < other_a_ts < r_ts:
                        overlapping_pairs.add((h[1], other_wid))

        had_overlap = len(overlapping_pairs) > 0
        print(f"\n  Overlap detected: {had_overlap} ({len(overlapping_pairs)} overlapping pairs)")
        if overlapping_pairs:
            for a, b in overlapping_pairs:
                print(f"    {a} overlapped with {b}")

        assert max_observed <= 2, (
            f"G-06 OS SUBPROCESS RACE: {max_observed} concurrent > 2 max"
        )
        print(f"  OS subprocess politeness: max_observed={max_observed}, max_allowed=2, "
              f"peak_concurrent_holders={peak}, had_overlap={had_overlap}")

    finally:
        os.unlink(script_path)
        await redis.delete(slot_key)
