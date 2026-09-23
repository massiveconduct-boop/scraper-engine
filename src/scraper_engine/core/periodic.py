# core/periodic.py
"""Shared periodic-loop runner for long-running supervisor daemons.

Extracted (round 34) from proxy/harvester_daemon.py, which originated this
exact pattern for its harvest/promotion/health/retention loops, so
orchestrator/webhook_sweeper.py can reuse it instead of re-implementing the
same isolate-failures-keep-running loop shape a second time.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from opentelemetry import trace

if TYPE_CHECKING:
    from scraper_engine.storage.redis_client import RedisClient

logger = logging.getLogger(__name__)

# Grace window before a job's heartbeat is considered stale, expressed as a
# multiple of its own interval — tolerates up to 2 full missed/slow cycles
# before api/health.py's daemon-liveness check flags it, so ordinary jitter
# or one slow cycle under load doesn't false-positive.
_HEARTBEAT_TTL_MULTIPLIER = 3


def heartbeat_key(name: str) -> str:
    """Redis key a periodic job's liveness heartbeat is stored under.

    Single source of truth shared by the writer (run_periodic below) and
    the reader (api/health.py's daemon-liveness check) so the two can't
    drift apart on key format.
    """
    return f"heartbeat:{name}"


async def run_periodic(
    name: str,
    cycle: Callable[[], Awaitable[Any]],
    interval_seconds: int,
    redis: RedisClient | None = None,
) -> None:
    """Run ``cycle()`` forever, once per ``interval_seconds``.

    A failure in one cycle is logged and swallowed so the loop keeps running —
    a transient network/DB error must not take the whole routine offline.
    Cancellation (graceful shutdown) is propagated.

    When ``redis`` is provided, writes a liveness heartbeat after every
    cycle *attempt* (success or swallowed failure alike — a cycle that
    keeps erroring but keeps attempting is a different, already-logged
    failure mode from a loop that's stopped ticking entirely). Consumed by
    api/health.py to answer "did this loop actually run recently," not
    just "does the process exist."
    """
    tracer = trace.get_tracer(__name__)
    while True:
        try:
            with tracer.start_as_current_span(f"periodic_{name}"):
                result = await cycle()
            logger.info("periodic_%s_cycle: %s", name, result)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("periodic_%s_cycle_failed", name)
        if redis is not None:
            try:
                await redis.raw.set(
                    heartbeat_key(name),
                    str(int(time.time())),
                    ex=interval_seconds * _HEARTBEAT_TTL_MULTIPLIER,
                )
            except Exception:
                logger.exception("periodic_%s_heartbeat_write_failed", name)
        await asyncio.sleep(interval_seconds)
