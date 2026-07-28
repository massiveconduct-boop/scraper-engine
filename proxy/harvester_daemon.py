# proxy/harvester_daemon.py
"""Long-running supervisor for the proxy subsystem's background routines.

The proxy package ships three routines that keep the proxy pool healthy but
previously had nothing to run them on a schedule (the `python -m proxy.harvester`
container command loaded a module with no entry point and exited immediately):

  - ``ProxyHarvester.harvest_once`` — collect + validate fresh proxies
  - ``ProxyPromotionJob.run_once``  — re-validate low-score proxies, promote winners
  - ``HealthMonitor.check_all``     — re-check live proxies against judge endpoints

This module is the missing supervisor: ``python -m proxy.harvester_daemon`` builds
all three from configuration and runs each on its own timer, isolating failures so
one bad cycle never kills the others, and shutting everything down cleanly on
SIGTERM/SIGINT (so ``docker compose stop`` is graceful).

Connection strings come from the single ``StorageConfig`` source of truth — the
same one the API and CLI use — so this process routes the DB through PgBouncer
like everything else.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
from collections.abc import Awaitable, Callable
from typing import Any

from opentelemetry import trace

from config.loader import load_config
from config.schema import AppConfig
from observability.bootstrap import bootstrap_observability
from proxy.asn_classifier import build_asn_classifier
from proxy.harvester import ProxyHarvester
from proxy.health_monitor import HealthMonitor
from proxy.promotion import ProxyPromotionJob
from proxy.retention_reaper import RetentionReaper
from storage.postgres_client import PostgresClient
from storage.redis_client import RedisClient

logger = logging.getLogger(__name__)


async def _run_periodic(
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
            with tracer.start_as_current_span(f"proxy_daemon_{name}"):
                result = await cycle()
            logger.info("proxy_daemon_%s_cycle: %s", name, result)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("proxy_daemon_%s_cycle_failed", name)
        await asyncio.sleep(interval_seconds)


async def run(config: AppConfig | None = None, stop: asyncio.Event | None = None) -> None:
    """Start the three loops and block until a stop signal arrives, then clean up.

    ``stop`` lets a caller (or a test) drive shutdown directly; when omitted the
    daemon installs SIGTERM/SIGINT handlers so ``docker compose stop`` is graceful.
    """
    cfg = config or load_config()
    bootstrap_observability(cfg.observability)
    ph = cfg.proxy_harvester

    pg = PostgresClient(cfg.storage.database_url)
    await pg.start()
    redis = RedisClient(redis_url=cfg.storage.redis_url)
    await redis.start()

    harvester = ProxyHarvester(pg, sources=ph.sources, asn_classifier=build_asn_classifier())
    # Promotion reuses the harvester's HTTP validator — no duplicated logic.
    promotion = ProxyPromotionJob(pg, ProxyHarvester._http_validate)
    health = HealthMonitor(pg, redis)
    reaper = RetentionReaper(pg, cfg.session_retention)

    tasks = [
        asyncio.create_task(
            _run_periodic("harvest", harvester.harvest_once, ph.interval_seconds)
        ),
        asyncio.create_task(
            _run_periodic("promotion", promotion.run_once, ph.promotion_interval_seconds)
        ),
        asyncio.create_task(
            _run_periodic("health", health.check_all, ph.health_interval_seconds)
        ),
        asyncio.create_task(
            _run_periodic(
                "retention", reaper.run_once, cfg.session_retention.cleanup_interval_seconds
            )
        ),
    ]
    logger.info(
        "proxy daemon started (harvest=%ss promotion=%ss health=%ss retention=%ss, sources=%s)",
        ph.interval_seconds,
        ph.promotion_interval_seconds,
        ph.health_interval_seconds,
        cfg.session_retention.cleanup_interval_seconds,
        ph.sources,
    )

    external_stop = stop is not None
    stop = stop or asyncio.Event()
    if not external_stop:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            # add_signal_handler is unavailable off the main thread / on some platforms.
            with contextlib.suppress(NotImplementedError):  # pragma: no cover
                loop.add_signal_handler(sig, stop.set)

    try:
        await stop.wait()
    finally:
        logger.info("proxy daemon stopping — cancelling loops")
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await redis.stop()
        await pg.stop()
        logger.info("proxy daemon stopped cleanly")


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
