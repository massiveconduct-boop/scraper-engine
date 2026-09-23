# browser/_xvfb_cleanup.py
"""Best-effort cleanup for botasaurus_driver's Xvfb lock/socket files.

Round 41: pyvirtualdisplay's AbstractDisplay.stop() (botasaurus_driver/
core/config.py -> pyvirtualdisplay.Display, the mechanism behind
enable_xvfb_virtual_display=True) SIGKILLs the Xvfb subprocess and waits
for it to exit, but never unlinks /tmp/.X<N>-lock or /tmp/.X11-unix/X<N>
afterward — SIGKILL bypasses Xvfb's own atexit cleanup, so both files are
left on disk even though the process is gone. camoufox/virtdisplay.py
(Camoufox's own hand-rolled equivalent) explicitly removes both after its
own kill() — this mirrors that.

Live-caught: with browser launches/closes already serialized (see
core/budget.py::XVFB_LOCK), a close-then-immediate-relaunch sequence
still transiently logged "SocketCreateListener() failed... server
already running" — Xvfb's own -displayfd flag retries internally against
its leftover socket file and self-heals (the fetch still succeeded), but
leaving the stale files in place wastes those retries every time and lets
orphans accumulate over a long-lived worker process's lifetime. Removing
them right after close closes that gap.
"""

from __future__ import annotations

import contextlib
import os
from typing import Any


def cleanup_stale_display(driver: Any) -> None:
    """Remove a just-closed botasaurus Driver's Xvfb lock/socket files, if any.

    Reaches into botasaurus_driver's private Config._display attribute —
    there's no public API for this. Best-effort only: any failure (attribute
    missing, file already gone, permission issue) is silently ignored, since
    this is opportunistic hygiene, not something a fetch's success depends on.
    """
    display_obj = getattr(getattr(driver, "config", None), "_display", None)
    display_nr = getattr(display_obj, "display", None)
    if display_nr is None:
        return
    for path in (f"/tmp/.X{display_nr}-lock", f"/tmp/.X11-unix/X{display_nr}"):
        with contextlib.suppress(OSError):
            os.remove(path)
