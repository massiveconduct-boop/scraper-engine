# orchestrator/stuck_job_reaper.py
"""Long-running supervisor that reconciles scrape_jobs rows stuck at
PROCESSING (round 52) or PENDING (round 54) forever.

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

Round 54 — while digging deeper into research_agent's logs the same way,
found a second, distinct stuck-forever shape: 17 real jobs stuck at
PENDING, some for 5+ days, none with a matching `rq:job:*` Redis key at
all. Root cause: `api/routes.py`'s job INSERT and its `_queue.enqueue()`
call are two separate operations, not one transaction — a transient Redis
error (or anything else raising) between them left the row committed as
PENDING with nothing ever actually queued, invisible to this reaper
before this round (which only ever looked at PROCESSING) and to the
caller, who'd just see a 500 with no way to know whether the job existed.
`api/routes.py` now catches that exception at the source and marks the
row FAILED immediately going forward (see its own round-54 comment); this
reaper's broadened PENDING sweep is the defense-in-depth half — it also
catches the case a try/except at the API layer structurally cannot: the
process getting killed at the exact instant between the INSERT committing
and the enqueue call running. Same reconciliation logic as PROCESSING
handles this correctly already (rq has no record at all of a job that was
never enqueued, which is already one of the two conditions this reaper
treats as reconcilable) — only the SQL candidate query needed to widen.

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

# Single source of truth for the queue name — orchestrator/job_queue.py is
# what actually enqueues, so deriving both the queue key and the registry
# keys from it keeps this check from drifting away from the real queue.
from .job_queue import QUEUE_NAME as _RQ_QUEUE_NAME

logger = logging.getLogger(__name__)

SWEEP_INTERVAL_SECONDS = 60
# A job's row is set to PROCESSING right before work starts and updated
# again the moment it finishes — a genuinely fast job could legitimately
# still be inside this window. Only reconcile rows well past ordinary
# completion time; never touch a row that might just be mid-flight.
_STALE_PROCESSING_GRACE_SECONDS = 120
# PENDING rows can legitimately sit longer under real queue backlog (3
# workers pulling from one shared queue) before a worker even starts them
# — more generous than the PROCESSING grace above, which only needs to
# tolerate the time between a worker picking a job up and its first DB
# write, not an entire wait-in-line.
_STALE_PENDING_GRACE_SECONDS = 300
_RQ_TERMINAL_STATUSES = {"failed", "finished", "stopped", "canceled"}
# Round 62 — the rq-side places a non-terminal job can legitimately be
# waiting. A job hash claiming "queued"/"started"/"deferred" while being
# absent from every one of these is unreachable: no worker can ever pick it
# up, because workers poll these structures, not the job hashes.
_RQ_QUEUE_KEY = f"rq:queue:{_RQ_QUEUE_NAME}"


def _rq_registry_zsets() -> tuple[str, ...]:
    """The rq registry keys a non-terminal job can legitimately sit in.

    Round 63 — these were hardcoded strings, and one of them was WRONG for
    the installed rq. rq 2.10's StartedJobRegistry key is
    `rq:wip:scraper-jobs`; the constant said `rq:started:scraper-jobs`, a key
    that simply does not exist here. So every genuinely RUNNING job looked
    unreachable, and any job still PROCESSING after the 120s grace was
    reconciled to FAILED underneath its own live worker.

    That is invisible for a single-URL job, which finishes inside the grace,
    and fatal for a multi-URL one, which cannot: an external consumer
    reported a 5-URL job that "never completed within ~7 minutes" and fell
    back to 95 serialized 1-URL jobs. Live-reproduced here — a 10-URL job was
    marked FAILED at 161s with `rq_status=started (orphaned: in no queue or
    registry)` while its worker went on fetching and writing results.

    Asking rq for its own key names removes the class of bug rather than the
    instance: an upgrade that renames a registry can no longer silently turn
    this check into "reap everything that is running".
    """
    from rq.registry import DeferredJobRegistry, ScheduledJobRegistry, StartedJobRegistry

    return tuple(
        registry(name=_RQ_QUEUE_NAME, connection=None).key
        for registry in (StartedJobRegistry, DeferredJobRegistry, ScheduledJobRegistry)
    )


async def _rq_job_status(redis: RedisClient, job_id: str) -> str | None:
    """None means rq has no record of this job at all anymore (expired
    failure_ttl/result_ttl, or an id that was never actually enqueued) —
    treated the same as a terminal status below: PROCESSING-forever in our
    own DB is a worse outcome than reconciling on that assumption."""
    raw = await redis.raw.hget(f"rq:job:{job_id}", "status")
    if raw is None:
        return None
    return raw.decode() if isinstance(raw, bytes) else raw


async def _rq_job_is_reachable(redis: RedisClient, job_id: str) -> bool:
    """True if a worker could still actually pick this job up.

    Round 62. A non-terminal status on the job HASH is not sufficient
    evidence that a job is alive, and trusting it alone was a real bug: 20
    rows sat PENDING in Postgres for five weeks (2026-08-12 to 2026-08-27)
    while the reaper logged `reconciled=0 still_processing=20` once a
    minute, forever. Their `rq:job:*` hashes existed, said `status=queued`,
    and carried TTL -1 — but the ids were in no queue and no registry, so
    nothing was ever going to run them. rq workers consume the queue list
    and the registries; an orphaned hash is invisible to them.

    Checking reachability instead of believing the hash turns that
    permanent stall into an ordinary reconcile. Deliberately fails SAFE:
    any Redis error here returns True (assume alive), because wrongly
    reconciling a genuinely queued job cancels real work, while wrongly
    skipping one only costs another 60s sweep.
    """
    try:
        if await redis.raw.lpos(_RQ_QUEUE_KEY, job_id) is not None:
            return True
        # Round 63 — a job that is RUNNING right now has a live execution
        # registry (rq.executions.ExecutionRegistry, `rq:executions:{job_id}`),
        # written when the work-horse starts and deleted when it finishes.
        # This is the cheapest and most direct "is anyone actually working on
        # this" signal rq offers, and it is keyed by the bare job id.
        if await redis.raw.exists(f"rq:executions:{job_id}"):
            return True
        for zset in _rq_registry_zsets():
            if await redis.raw.zscore(zset, job_id) is not None:
                return True
            # StartedJobRegistry's members are NOT bare job ids — rq's own
            # docstring: "Each entry is a {job_id}:{execution_id}". So the
            # zscore above structurally cannot match a started job, and this
            # check reported every running job as an orphan even once the key
            # name was right. Matching the prefix covers both member shapes
            # without having to know which registry uses which.
            cursor, found = await redis.raw.zscan(zset, 0, match=f"{job_id}:*", count=100)
            while True:
                if found:
                    return True
                if cursor == 0:
                    break
                cursor, found = await redis.raw.zscan(
                    zset, cursor, match=f"{job_id}:*", count=100
                )
    except Exception:
        logger.exception("rq_reachability_check_failed job_id=%s", job_id)
        return True
    return False


async def _reconcile_tenant(
    pg: PostgresClient, redis: RedisClient, tenant: TenantId, cfg: AppConfig
) -> tuple[int, int]:
    """Returns (reconciled_count, still_genuinely_processing_count)."""
    rows = await pg.fetch(
        tenant,
        """SELECT job_id, webhook_url FROM scrape_jobs
           WHERE (status = $1 AND updated_at < NOW() - make_interval(secs => $2))
              OR (status = $3 AND updated_at < NOW() - make_interval(secs => $4))""",
        JobStatus.PROCESSING.value,
        _STALE_PROCESSING_GRACE_SECONDS,
        JobStatus.PENDING.value,
        _STALE_PENDING_GRACE_SECONDS,
    )
    reconciled = 0
    still_processing = 0
    for row in rows:
        job_id = str(row["job_id"])
        rq_status = await _rq_job_status(redis, job_id)
        if rq_status is not None and rq_status not in _RQ_TERMINAL_STATUSES:
            # Round 62 — a non-terminal status is only believable if the job
            # is still reachable by a worker. See _rq_job_is_reachable.
            if await _rq_job_is_reachable(redis, job_id):
                still_processing += 1
                continue
            rq_status = f"{rq_status} (orphaned: in no queue or registry)"

        # finished_at (round 63): a reaped job is one whose worker died
        # without running its own failure path, so this is the only place
        # its end time will ever be recorded. Left NULL, GET /v1/jobs/{id}
        # reports runtime_ms=None for exactly the jobs being investigated.
        await pg.execute(
            tenant,
            "UPDATE scrape_jobs SET status = $1, updated_at = NOW(), "
            "finished_at = COALESCE(finished_at, NOW()) WHERE job_id = $2::uuid",
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
