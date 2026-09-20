# core/startup.py
"""Dependency-wait helper for process startup (round 62).

Distinct from core/retry.py, which is the *fetch* retry matrix: bounded
attempt counts per FailureCategory, tuned so a doomed scrape gives up fast
and frees the worker. Startup has the opposite requirement. A process that
cannot reach Postgres yet is not failing — it is early. Giving up turns a
recoverable ordering problem into a permanent outage.

Live evidence this exists for (ops/research/itel-30000mah-jumia,
20 Sep 2026): the API container came up before Postgres/PgBouncer resolved,
raised out of its lifespan, and exited. supervisord restarted it, it lost
the race again, and after `startretries` consecutive fast exits supervisord
marked the program FATAL and stopped trying. Postgres came up seconds
later; MinIO came up after that. Nothing was wrong with the deployment by
then — but the API stayed dead until a human restarted it by hand, and the
whole stack sat down for roughly three weeks.

The fix has to hold at both layers, because either one alone still fails:
  * here — the process waits instead of exiting, so supervisord's restart
    counter is never incremented in the first place;
  * docker/supervisord.conf — `startretries` is raised far beyond any
    plausible transient-outage burst, so a crash mode this helper does NOT
    cover (an import error, an OOM kill) still gets retried indefinitely
    rather than latching FATAL forever.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Coroutine
from typing import TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")

# Attempt N sleeps min(BASE * 2**N, MAX). Reaches the 30s ceiling on attempt
# 6, so a dependency that takes a minute costs ~8 log lines rather than a
# tight spin, and one that takes an hour costs ~120.
_BASE_DELAY_SECONDS = 0.5
_MAX_DELAY_SECONDS = 30.0

# Attempts before the per-failure log escalates WARNING -> ERROR. Below this
# we are almost certainly just losing a compose start-order race, which is
# expected and self-healing; past it the far likelier explanation is a real
# misconfiguration (wrong DSN, wrong credentials, wrong network) that no
# amount of waiting fixes, and an operator needs to see it at ERROR. The
# process still keeps retrying either way — see the module docstring for why
# exiting is never the right answer here.
_ESCALATE_AFTER_ATTEMPTS = 10


async def wait_for_dependency(
    name: str,
    connect: Callable[[], Coroutine[None, None, T]],
    *,
    max_attempts: int | None = None,
) -> T:
    """Call `connect` until it succeeds, backing off exponentially.

    Args:
        name: Human-readable dependency name for log lines ("postgres").
        connect: Async zero-arg callable that raises on failure. Must be
            safe to call repeatedly — every client used here
            (PostgresClient/RedisClient/S3Client `.start()`) builds its
            pool fresh per call, so a failed attempt leaves nothing behind.
        max_attempts: None (the default) retries forever, which is what
            every production caller wants. Tests pass a small integer so a
            permanently-unreachable dependency fails the assertion instead
            of hanging the suite.

    Returns:
        Whatever `connect` returns on its first successful call.

    Raises:
        The last exception raised by `connect`, but only when `max_attempts`
        is set and exhausted. With the default, this function does not
        raise — it waits.
    """
    attempt = 0
    while True:
        try:
            result = await connect()
        except Exception as exc:
            attempt += 1
            if max_attempts is not None and attempt >= max_attempts:
                logger.error(
                    "startup dependency unavailable, giving up",
                    extra={"dependency": name, "attempts": attempt, "error": str(exc)},
                )
                raise
            delay = min(_BASE_DELAY_SECONDS * (2**attempt), _MAX_DELAY_SECONDS)
            log = logger.error if attempt >= _ESCALATE_AFTER_ATTEMPTS else logger.warning
            log(
                "startup dependency unavailable, retrying",
                extra={
                    "dependency": name,
                    "attempt": attempt,
                    "retry_in_seconds": delay,
                    "error": str(exc),
                },
            )
            await asyncio.sleep(delay)
            continue
        if attempt:
            logger.info(
                "startup dependency connected after retries",
                extra={"dependency": name, "attempts": attempt + 1},
            )
        return result
