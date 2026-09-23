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

from opentelemetry import trace

from scraper_engine.config.loader import load_config
from scraper_engine.config.schema import AppConfig
from scraper_engine.core.periodic import run_periodic as _run_periodic
from scraper_engine.observability.bootstrap import bootstrap_observability
from scraper_engine.proxy.asn_classifier import build_asn_classifier
from scraper_engine.proxy.harvester import ProxyHarvester
from scraper_engine.proxy.health_monitor import HealthMonitor
from scraper_engine.proxy.manager import HARVEST_KICK_KEY
from scraper_engine.proxy.pool_health import PoolHealthMonitor
from scraper_engine.proxy.promotion import ProxyPromotionJob
from scraper_engine.proxy.retention_reaper import RetentionReaper
from scraper_engine.storage.postgres_client import PostgresClient
from scraper_engine.storage.redis_client import RedisClient

logger = logging.getLogger(__name__)

# Kick-watcher tuning (round 34) — see proxy/manager.py::_signal_exhaustion for
# the producer side. Poll interval is fast relative to the steady-state
# harvest cadence (ph.interval_seconds, default 600s) because the whole point
# is reacting to real demand faster than the timer would. Cooldown is
# independent of and longer than the kick TTL (proxy/manager.py's
# HARVEST_KICK_TTL_SECONDS=30) so a flood of repeated exhaustions — even
# spanning multiple kick-key TTL windows — still can't run more than one
# out-of-band harvest per minute.
KICK_POLL_INTERVAL_SECONDS = 5
HARVEST_COOLDOWN_KEY = "proxy:harvest:cooldown"
HARVEST_COOLDOWN_SECONDS = 60


async def _run_kick_watcher(harvester: ProxyHarvester, redis: RedisClient) -> None:
    """Poll for proxy/manager.py's debounced exhaustion signal and run an
    out-of-band harvest cycle when one is pending (round 34). This is what
    actually closes the "pool doesn't refill itself" gap — the steady-state
    _run_periodic("harvest", ...) loop above still runs on its own timer
    regardless, this just runs an extra cycle sooner when real demand says
    the pool is empty. The cooldown key (not the kick key's own TTL) is the
    thing that rate-limits actual harvest_once() calls; see the module
    docstring above for why the two have independent lifetimes."""
    tracer = trace.get_tracer(__name__)
    while True:
        try:
            kicked = await redis.raw.get(HARVEST_KICK_KEY)
            if kicked:
                cooldown_acquired = await redis.raw.set(
                    HARVEST_COOLDOWN_KEY, "1", nx=True, ex=HARVEST_COOLDOWN_SECONDS
                )
                if cooldown_acquired:
                    # Consume the kick immediately so a fresh exhaustion right
                    # after this cycle can signal again without waiting out
                    # the rest of the original kick's TTL.
                    await redis.raw.delete(HARVEST_KICK_KEY)
                    with tracer.start_as_current_span("proxy_daemon_kick_harvest"):
                        result = await harvester.harvest_once()
                    logger.info("proxy_daemon_kick_harvest_cycle: %s", result)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("proxy_daemon_kick_watcher_failed")
        await asyncio.sleep(KICK_POLL_INTERVAL_SECONDS)


_TRANSITION_EVENT_TYPE: dict[str, str] = {
    "critical": "proxy_pool.critical",
    "degraded": "proxy_pool.degraded",
    "healthy": "proxy_pool.recovered",
}


async def _pool_health_cycle(
    monitor: PoolHealthMonitor, cfg: AppConfig, pg: PostgresClient, redis: RedisClient
) -> list[str]:
    """One health-check cycle: recompute per-tier state, log every real
    transition, and — when cfg.webhook.ops_webhook_url is configured — push
    each transition to the operator-facing webhook channel (round 34). This
    is the direct fix for "proxy exhaustion has no notification path at
    all": before this, nothing about pool health ever reached a webhook,
    correctly firing or not. Kept as its own function (rather than inlined
    into run()) so _run_periodic's generic try/except/log wrapper covers it
    the same as every other routine."""
    from scraper_engine.core.tenant import TenantId
    from scraper_engine.orchestrator.webhook_dispatch import enqueue_and_deliver_webhook_event
    from scraper_engine.orchestrator.webhook_events import WebhookEvent, WebhookEventType

    system = TenantId("system")
    transitions = await monitor.check(system)
    for t in transitions:
        logger.warning(
            "proxy_pool_health_transition tier=%s %s->%s validated_count=%d",
            t.tier,
            t.old_state.value,
            t.new_state.value,
            t.validated_count,
        )
        if cfg.webhook.ops_webhook_url:
            event = WebhookEvent(
                event_type=WebhookEventType(_TRANSITION_EVENT_TYPE[t.new_state.value]),
                tenant_id=str(system),
                job_id=None,
                payload={
                    "tier": t.tier,
                    "old_state": t.old_state.value,
                    "new_state": t.new_state.value,
                    "validated_count": t.validated_count,
                },
            )
            await enqueue_and_deliver_webhook_event(
                cfg, pg, redis, system, cfg.webhook.ops_webhook_url, event
            )
    return [f"tier{t.tier}:{t.old_state.value}->{t.new_state.value}" for t in transitions]


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

    harvester = ProxyHarvester(
        pg, sources=ph.sources, asn_classifier=build_asn_classifier(), redis=redis
    )
    # Promotion reuses the harvester's HTTP validator — no duplicated logic.
    # Same classifier construction as the harvester above (round 32 — needed
    # so promoted proxies get a real ASN-informed score instead of always
    # "unknown").
    promotion = ProxyPromotionJob(
        pg, ProxyHarvester._http_validate, asn_classifier=build_asn_classifier()
    )
    health = HealthMonitor(pg, redis, asn_classifier=build_asn_classifier())
    reaper = RetentionReaper(pg, cfg.session_retention)
    pool_health = PoolHealthMonitor(pg, redis, cfg.proxy_tiers)

    tasks = [
        asyncio.create_task(
            _run_periodic("harvest", harvester.harvest_once, ph.interval_seconds, redis=redis)
        ),
        asyncio.create_task(
            _run_periodic(
                "promotion", promotion.run_once, ph.promotion_interval_seconds, redis=redis
            )
        ),
        asyncio.create_task(
            _run_periodic("health", health.check_all, ph.health_interval_seconds, redis=redis)
        ),
        asyncio.create_task(
            _run_periodic(
                "pool_health",
                lambda: _pool_health_cycle(pool_health, cfg, pg, redis),
                ph.health_interval_seconds,
                redis=redis,
            )
        ),
        asyncio.create_task(
            _run_periodic(
                "retention",
                reaper.run_once,
                cfg.session_retention.cleanup_interval_seconds,
                redis=redis,
            )
        ),
        asyncio.create_task(_run_kick_watcher(harvester, redis)),
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


if __name__ == "__main__":  # pragma: no cover — only true under `python -m`, not tests
    main()
