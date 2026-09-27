"""core/leftover_processes.py — the sweeps that kill browser processes a job
left running (547 chromium + 120 Xvfb across three workers, 2026-09-27).

The reaper tests use real child processes: the point is that they are
really gone afterwards, not that a mock was called.
"""

import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import psutil
import pytest

from scraper_engine.core import leftover_processes as lp


@pytest.mark.parametrize(
    ("name", "kind"),
    [
        ("chromium", "chromium"),
        ("chrome_crashpad_handler", "chromium"),
        ("Xvfb", "xvfb"),
        ("camoufox-bin", "camoufox"),
        ("firefox", "camoufox"),
        ("node", "node"),
        ("python", "other"),
    ],
)
def test_process_kind(name, kind):
    assert lp.process_kind(name) == kind


# Runs in its own process, so the sweep can only reach processes this script
# started — never another child of the test runner. It starts a child that
# starts its own child (the shape of Chrome and its renderers), sweeps, and
# reports what it killed and what is still alive below it.
_REAP_SCRIPT = """
import json, subprocess, sys, time
import psutil
from scraper_engine.core.leftover_processes import reap_descendants
code = "import subprocess, sys, time; " \\
    "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); time.sleep(60)"
subprocess.Popen([sys.executable, "-c", code])
deadline = time.monotonic() + 10
while len(psutil.Process().children(recursive=True)) < 2 and time.monotonic() < deadline:
    time.sleep(0.05)
counts = reap_descendants()
alive = [p for p in psutil.Process().children(recursive=True)
         if p.status() != psutil.STATUS_ZOMBIE]
print(json.dumps({"counts": counts, "alive": len(alive)}))
"""


def test_reap_descendants_kills_every_live_process_below_this_one():
    import json

    out = subprocess.run(
        [sys.executable, "-c", _REAP_SCRIPT], capture_output=True, text=True, timeout=30
    )
    assert out.returncode == 0, out.stderr
    result = json.loads(out.stdout)
    assert result == {"counts": {"other": 2}, "alive": 0}


def test_reap_descendants_skips_zombies_and_vanished_processes(monkeypatch):
    live = MagicMock(status=MagicMock(return_value=psutil.STATUS_SLEEPING))
    live.name.return_value = "Xvfb"
    zombie = MagicMock(status=MagicMock(return_value=psutil.STATUS_ZOMBIE))
    vanished = MagicMock(status=MagicMock(side_effect=psutil.NoSuchProcess(1)))
    me = MagicMock(children=MagicMock(return_value=[live, zombie, vanished]))
    monkeypatch.setattr(lp.psutil, "Process", lambda: me)
    monkeypatch.setattr(lp.psutil, "wait_procs", MagicMock())
    assert lp.reap_descendants() == {"xvfb": 1}
    live.kill.assert_called_once()
    zombie.kill.assert_not_called()
    vanished.kill.assert_not_called()


def test_kill_skips_a_process_that_is_already_gone():
    gone = MagicMock(name=MagicMock(return_value="Xvfb"))
    gone.kill.side_effect = psutil.NoSuchProcess(1)
    assert lp._kill([gone]) == {}


def _proc(ppid, name, uid, status=psutil.STATUS_SLEEPING, uids=True):
    proc = MagicMock()
    proc.info = {
        "ppid": ppid,
        "name": name,
        "uids": SimpleNamespace(real=uid) if uids else None,
        "status": status,
    }
    proc.name.return_value = name
    return proc


def test_reap_orphaned_browsers_kills_only_orphaned_browsers_of_this_user(monkeypatch):
    import os

    uid = os.getuid()
    orphan_xvfb = _proc(1, "Xvfb", uid)
    orphan_chrome = _proc(1, "chromium", uid)
    live_chrome = _proc(4242, "chromium", uid)  # a running job's browser
    rq_worker = _proc(1, "rq", uid)  # PID 1's other child: not a browser
    other_user = _proc(1, "Xvfb", uid + 1)
    zombie = _proc(1, "chromium", uid, status=psutil.STATUS_ZOMBIE)
    no_uids = _proc(1, "Xvfb", uid, uids=False)
    no_name = _proc(1, None, uid)
    # Chrome double-forks its crash reporter: PID 1 is its parent even while
    # its browser runs, and it exits with that browser.
    crashpad = _proc(1, "chrome_crashpad_handler", uid)
    procs = [orphan_xvfb, orphan_chrome, live_chrome, rq_worker, other_user, zombie, no_uids]
    procs += [no_name, crashpad]
    monkeypatch.setattr(lp.psutil, "process_iter", lambda attrs: procs)
    monkeypatch.setattr(lp.psutil, "wait_procs", MagicMock())
    assert lp.reap_orphaned_browsers() == {"xvfb": 1, "chromium": 1}
    orphan_xvfb.kill.assert_called_once()
    orphan_chrome.kill.assert_called_once()
    for spared in (live_chrome, rq_worker, other_user, zombie, no_uids, no_name, crashpad):
        spared.kill.assert_not_called()


def test_count_browser_processes(monkeypatch):
    procs = [
        _proc(1, "chromium", 0),
        _proc(1, "chromium", 0),
        _proc(1, "chromium", 0, status=psutil.STATUS_ZOMBIE),
        _proc(1, "Xvfb", 0),
        _proc(1, "python", 0),
        _proc(1, None, 0),
    ]
    monkeypatch.setattr(lp.psutil, "process_iter", lambda attrs: procs)
    assert lp.count_browser_processes() == {"chromium": 2, "xvfb": 1, "camoufox": 0, "node": 0}


class TestSweep:
    def test_logs_and_publishes_what_it_killed(self, monkeypatch, caplog):
        monkeypatch.setattr(lp, "count_browser_processes", lambda: {"chromium": 0, "xvfb": 0})
        monkeypatch.setattr(lp.socket, "gethostname", lambda: "worker-l2")
        redis = MagicMock()
        pipe = redis.pipeline.return_value

        def reap_descendants():
            return {"xvfb": 1, "chromium": 9}

        assert lp.sweep(reap_descendants, redis) == {"xvfb": 1, "chromium": 9}
        assert "leftover_processes_killed reaper=reap_descendants" in caplog.text
        pipe.incrby.assert_any_call("metrics:browser_processes_reaped_total:xvfb", 1)
        pipe.incrby.assert_any_call("metrics:browser_processes_reaped_total:chromium", 9)
        pipe.hset.assert_any_call("metrics:worker_browser_processes:worker-l2", "chromium", 0)
        pipe.hset.assert_any_call("metrics:worker_browser_processes:worker-l2", "xvfb", 0)
        pipe.expire.assert_called_once()
        pipe.execute.assert_called_once()

    def test_nothing_killed_still_publishes_the_live_count(self, monkeypatch, caplog):
        monkeypatch.setattr(lp, "count_browser_processes", lambda: {"chromium": 3})
        redis = MagicMock()
        assert lp.sweep(dict, redis) == {}
        assert "leftover_processes_killed" not in caplog.text
        redis.pipeline.return_value.incrby.assert_not_called()
        redis.pipeline.return_value.hset.assert_called_once()

    def test_without_redis_only_reaps(self):
        assert lp.sweep(lambda: {"xvfb": 1}, None) == {"xvfb": 1}

    def test_a_failing_reaper_never_raises(self, caplog):
        def broken():
            raise psutil.AccessDenied(1)

        assert lp.sweep(broken, MagicMock()) == {}
        assert "leftover_process_sweep_failed" in caplog.text

    def test_a_redis_failure_never_raises(self, monkeypatch, caplog):
        monkeypatch.setattr(lp, "count_browser_processes", lambda: {})
        redis = MagicMock()
        redis.pipeline.return_value.execute.side_effect = ConnectionError("redis down")
        assert lp.sweep(lambda: {"xvfb": 1}, redis) == {"xvfb": 1}
        assert "leftover_process_metrics_publish_failed" in caplog.text
