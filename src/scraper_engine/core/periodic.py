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
from collections.abc import Awaitable, Callable
from typing import Any

from opentelemetry import trace

logger = logging.getLogger(__name__)


async def run_periodic(
    name: str,
    cycle: Callable[[], Awaitable[Any]],
    interval_seconds: int,
) -> None:
    """Run ``cycle()`` forever, once per ``interval_seconds``.

    A failure in one cycle is logged and swallowed so the loop keeps running —
    a transient network/DB error must not take the whole routine offline.
    Cancellation (graceful shutdown) is propagated.
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
        await asyncio.sleep(interval_seconds)
