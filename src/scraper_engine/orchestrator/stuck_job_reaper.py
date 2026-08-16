# orchestrator/stuck_job_reaper.py
"""Long-running supervisor that reconciles scrape_jobs rows stuck at
PROCESSING forever (round 52).

Root cause (confirmed against the real installed rq 2.10 source, not
guessed): rq enforces job_timeout via an in-process SIGALRM
(UnixSignalDeathPenalty) inside the forked "work horse" process running
orchestrator/tasks.py::_run_scrape_job. That raises a catchable
JobTimeoutException, which tasks.py's own `except Exception:` block is
meant to catch and use to mark scrape_jobs.status = FAILED. But
Worker.monitor_work_horse (the PARENT process) has its own second-tier
safety net: if the horse hasn't actually exited within
`job.timeout + 60s` of that — plausible here given Camoufox/Playwright/
Xvfb subprocess waits, which can hold the GIL or block in a C-level call a
Python signal can't interrupt until it returns — the parent SIGKILLs the
horse directly (`kill_horse()`). A SIGKILL cannot be caught by anything
running inside that process: tasks.py's except/finally never runs. Worse,
rq's own `on_failure` callback mechanism ALSO would not have closed this
gap — traced `Worker.handle_job_failure` (the path taken after a SIGKILL)
and confirmed it never calls `job.execute_failure_callback`; that only
fires from inside `perform_job`'s in-process except block, the same path
that's already bypassed. Live evidence: 16 real research_agent jobs found
stuck at PROCESSING in Postgres while rq's own Redis-side job hash already
shows `status=failed` with an empty `worker_name` — the parent-kill
signature.

The only reliable source of truth left after a hard kill is rq's own
Redis-side job status, which IS updated correctly by the parent even in
this path (`Worker.handle_job_failure` still runs, just not the
in-process callback). This reaper periodically finds scrape_jobs rows
stuck at PROCESSING past a grace window, cross-checks rq's actual status
for that job_id, and reconciles our DB (firing the tenant's webhook
through the same durable-outbox path tasks.py itself uses) whenever rq's
bookkeeping disagrees with ours.

``python -m scraper_engine.orchestrator.stuck_job_reaper`` is the entry
point docker-compose.yml's stuck-job-reaper program (docker/
supervisord.conf) runs.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal

from scraper_engine.config.loader import load_config
from scraper_engine.config.schema import AppConfig
from scraper_engine.core.models import JobStatus
from scraper_engine.core.periodic import run_periodic
from scraper_engine.core.tenant import TenantId
from scraper_engine.observability.bootstrap import bootstrap_observability
from scraper_engine.storage.postgres_client import PostgresClient
from scraper_engine.storage.redis_client import RedisClient

logger = logging.getLogger(__name__)

SWEEP_INTERVAL_SECONDS = 60
# A job's row is set to PROCESSING right before work starts and updated
# again the moment it finishes — a genuinely fast job could legitimately
# still be inside this window. Only reconcile rows well past ordinary
# completion time; never touch a row that might just be mid-flight.
_STALE_GRACE_SECONDS = 120
_RQ_TERMINAL_STATUSES = {"failed", "finished", "stopped", "canceled"}


async def _rq_job_status(redis: RedisClient, job_id: str) -> str | None:
    """None means rq has no record of this job at all anymore (expired
    failure_ttl/result_ttl, or an id that was never actually enqueued) —
    treated the same as a terminal status below: PROCESSING-forever in our
    own DB is a worse outcome than reconciling on that assumption."""
    raw = await redis.raw.hget(f"rq:job:{job_id}", "status")
    if raw is None:
        return None
    return raw.decode() if isinstance(raw, bytes) else raw


async def _reconcile_tenant(
    pg: PostgresClient, redis: RedisClient, tenant: TenantId, cfg: AppConfig
) -> tuple[int, int]:
    """Returns (reconciled_count, still_genuinely_processing_count)."""
    rows = await pg.fetch(
        tenant,
        """SELECT job_id, webhook_url FROM scrape_jobs
           WHERE status = $1 AND updated_at < NOW() - make_interval(secs => $2)""",
        JobStatus.PROCESSING.value,
        _STALE_GRACE_SECONDS,
    )
    reconciled = 0
    still_processing = 0
    for row in rows:
        job_id = str(row["job_id"])
        rq_status = await _rq_job_status(redis, job_id)
        if rq_status is not None and rq_status not in _RQ_TERMINAL_STATUSES:
            still_processing += 1
            continue

        await pg.execute(
            tenant,
            "UPDATE scrape_jobs SET status = $1, updated_at = NOW() WHERE job_id = $2::uuid",
            JobStatus.FAILED.value,
            job_id,
        )
        logger.warning(
            "stuck_job_reconciled job_id=%s tenant=%s rq_status=%s",
            job_id,
            tenant,
            rq_status,
        )
        reconciled += 1

        webhook_url = row["webhook_url"]
        if webhook_url:
            from scraper_engine.orchestrator.tasks import _dispatch_job_webhook

            try:
                await _dispatch_job_webhook(
                    cfg,
                    pg,
                    redis,
                    tenant,
                    webhook_url,
                    job_id,
                    JobStatus.FAILED,
                    [],
                    "job exceeded its timeout and the worker was terminated "
                    "before it could report failure — reconciled by "
                    "stuck_job_reaper",
                    partial_failure=False,
                )
            except Exception:
                logger.exception("stuck_job_reconcile_webhook_failed job_id=%s", job_id)

    return reconciled, still_processing


async def _sweep_cycle(pg: PostgresClient, redis: RedisClient, cfg: AppConfig) -> str:
    """One sweep across every tenant schema. One tenant's schema being
    unreachable must not block the others — same isolation contract every
    other per-tenant-schema sweep in this codebase already follows
    (webhook_sweeper.py, retention_reaper.py)."""
    total_reconciled = 0
    total_still_processing = 0

    rows = await pg.fetch(TenantId("system"), "SELECT tenant_id FROM public.tenants")
    for row in rows:
        tenant = TenantId(row["tenant_id"])
        try:
            reconciled, still_processing = await _reconcile_tenant(pg, redis, tenant, cfg)
            total_reconciled += reconciled
            total_still_processing += still_processing
        except Exception:
            logger.exception("stuck_job_reap_tenant_failed tenant=%s", tenant)

    return f"reconciled={total_reconciled} still_processing={total_still_processing}"


async def run(config: AppConfig | None = None, stop: asyncio.Event | None = None) -> None:
    """Start the sweep loop and block until a stop signal arrives.

    ``stop`` lets a caller (or a test) drive shutdown directly; when
    omitted the daemon installs SIGTERM/SIGINT handlers so
    ``docker compose stop`` is graceful — same shape as
    orchestrator/webhook_sweeper.py::run.
    """
    cfg = config or load_config()
    bootstrap_observability(cfg.observability)

    pg = PostgresClient(cfg.storage.database_url)
    await pg.start()
    redis = RedisClient(redis_url=cfg.storage.redis_url)
    await redis.start()

    task = asyncio.create_task(
        run_periodic(
            "stuck_job_reap",
            lambda: _sweep_cycle(pg, redis, cfg),
            SWEEP_INTERVAL_SECONDS,
            redis=redis,
        )
    )
    logger.info("stuck job reaper started (interval=%ss)", SWEEP_INTERVAL_SECONDS)

    external_stop = stop is not None
    stop = stop or asyncio.Event()
    if not external_stop:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            with contextlib.suppress(NotImplementedError):  # pragma: no cover
                loop.add_signal_handler(sig, stop.set)

    try:
        await stop.wait()
    finally:
        logger.info("stuck job reaper stopping")
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await redis.stop()
        await pg.stop()
        logger.info("stuck job reaper stopped cleanly")


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":  # pragma: no cover — only true under `python -m`, not tests
    main()
