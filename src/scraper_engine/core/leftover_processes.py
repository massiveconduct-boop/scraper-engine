"""Kill browser processes a job left behind, and count what is running.

rq runs every job in its own forked work-horse process, so when a job ends,
every process it started should be gone. Any that is not was leaked by some
path: a botasaurus close that failed partway (browser/_botasaurus_close.py),
a Camoufox teardown given up on after its timeout
(browser/camoufox_wrapper.py::_shutdown_browser_bounded), a launch still
running in an executor thread when its fetch was cancelled. Before this
existed, workers kept every such process until the container restarted:
547 chromium and 120 Xvfb processes across three workers over 36 hours
(2026-09-27).

Two sweeps, both run by orchestrator/tasks.py::run_scrape_job:

- `reap_descendants()` at job end kills every process still under the
  work-horse. Nothing under it is legitimate by then.
- `reap_orphaned_browsers()` at job start kills browser processes whose
  parent is PID 1. That is where a process ends up when the horse that
  started it is gone without closing it — rq kills a horse that overruns its
  timeout with a group kill, and Camoufox's Xvfb (started with
  start_new_session) is outside that group. A live job's browsers always
  have that job's horse (or their own browser) as parent, never PID 1.

`count_browser_processes()` feeds the `worker_browser_processes` gauge.
"""

from __future__ import annotations

import logging
import os
import socket
from typing import TYPE_CHECKING

import psutil

if TYPE_CHECKING:
    from collections.abc import Callable

    from redis import Redis

logger = logging.getLogger(__name__)

# Labels for the gauge and the reaped counter. "other" is anything a job
# left behind that is not one of the browser kinds (still killed, since a
# finished job owns no processes).
KINDS = ("chromium", "xvfb", "camoufox", "node", "other")

_WAIT_SECONDS = 5
# Refreshed at every job start and end; a worker that stops running jobs
# drops out of the gauge a day later instead of showing its last count forever.
_GAUGE_TTL_SECONDS = 86400
GAUGE_KEY_PREFIX = "metrics:worker_browser_processes:"
REAPED_KEY_PREFIX = "metrics:browser_processes_reaped_total:"


def process_kind(name: str) -> str:
    """Which kind of browser process a process name is ("other" if none)."""
    lowered = name.lower()
    if lowered.startswith("chrom"):
        return "chromium"
    if lowered == "xvfb":
        return "xvfb"
    if lowered.startswith(("camoufox", "firefox")):
        return "camoufox"
    if lowered == "node":
        # The Playwright driver behind Camoufox, and botasaurus's proxy-chain
        # helper for authenticated proxies.
        return "node"
    return "other"


def _kill(procs: list[psutil.Process]) -> dict[str, int]:
    """SIGKILL each process, wait for them to go, return counts by kind."""
    killed: list[psutil.Process] = []
    counts: dict[str, int] = {}
    for proc in procs:
        try:
            kind = process_kind(proc.name())
            proc.kill()
        except psutil.Error:
            continue
        killed.append(proc)
        counts[kind] = counts.get(kind, 0) + 1
    psutil.wait_procs(killed, timeout=_WAIT_SECONDS)
    return counts


def reap_descendants() -> dict[str, int]:
    """Kill every live process below this one. Zombies are skipped: they are
    already dead, and whoever their parent is (or PID 1) reaps them."""
    procs: list[psutil.Process] = []
    for proc in psutil.Process().children(recursive=True):
        try:
            if proc.status() != psutil.STATUS_ZOMBIE:
                procs.append(proc)
        except psutil.Error:
            continue
    return _kill(procs)


def reap_orphaned_browsers() -> dict[str, int]:
    """Kill browser processes of this user that have been handed to PID 1.

    Chrome's crash reporter is the exception: Chrome double-forks it, so its
    parent is PID 1 even while its browser is alive (seen live, 2026-09-28),
    and it exits with that browser on its own.
    """
    uid = os.getuid()
    procs: list[psutil.Process] = []
    for proc in psutil.process_iter(["ppid", "name", "uids", "status"]):
        info = proc.info
        name = info["name"] or ""
        if (
            info["ppid"] == 1
            and info["status"] != psutil.STATUS_ZOMBIE
            and info["uids"] is not None
            and info["uids"].real == uid
            and process_kind(name) != "other"
            and not name.startswith("chrome_crashpad")
        ):
            procs.append(proc)
    return _kill(procs)


def count_browser_processes() -> dict[str, int]:
    """Live browser processes visible to this process, by kind (a container
    sees only its own)."""
    counts = dict.fromkeys(KINDS[:-1], 0)
    for proc in psutil.process_iter(["name", "status"]):
        kind = process_kind(proc.info["name"] or "")
        if kind != "other" and proc.info["status"] != psutil.STATUS_ZOMBIE:
            counts[kind] += 1
    return counts


def sweep(reaper: Callable[[], dict[str, int]], redis: Redis | None) -> dict[str, int]:
    """Run one of the reapers above, log what it killed, and publish the
    reaped counts and the current live counts to Redis for /metrics.

    Never raises: this runs in a job's start and `finally`, and must not turn
    a finished job into a failed one.
    """
    reaper_name = reaper.__name__
    try:
        reaped = reaper()
    except Exception:
        logger.exception("leftover_process_sweep_failed reaper=%s", reaper_name)
        return {}
    if reaped:
        logger.error(
            "leftover_processes_killed reaper=%s counts=%s — a job did not close "
            "every browser it started",
            reaper_name,
            reaped,
        )
    if redis is None:
        return reaped
    try:
        pipe = redis.pipeline()
        for kind, n in reaped.items():
            pipe.incrby(f"{REAPED_KEY_PREFIX}{kind}", n)
        gauge_key = f"{GAUGE_KEY_PREFIX}{socket.gethostname()}"
        for kind, n in count_browser_processes().items():
            pipe.hset(gauge_key, kind, n)
        pipe.expire(gauge_key, _GAUGE_TTL_SECONDS)
        pipe.execute()
    except Exception:
        logger.warning("leftover_process_metrics_publish_failed", exc_info=True)
    return reaped
