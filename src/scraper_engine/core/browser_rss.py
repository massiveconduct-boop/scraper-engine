# core/browser_rss.py
"""What a live browser on this host actually costs in memory (round 67).

`capacity_controller.py` sizes the host's browser budget partly from free
memory, charging `host_capacity.browser_memory_mb` (1200) per browser. That
constant came from measured peaks on a trivial page (Camoufox 916 MiB,
Botasaurus 1167 MiB), and on this project's own host it is what binds: 4
cores, 23.9 GB total but ~10.5 GB MemAvailable (other workloads hold the
rest), so the target stopped near 9 browsers where an unlimited run drove
14-21 of them at CPU PSI 15-19.

So charge what the browsers weigh instead of what they might weigh. Each
worker samples its own live browser processes and publishes the mean; the
controller averages what every worker on the host reported. The reading can
only make the host MORE conservative than the constant — it is clamped to
`[min_browser_memory_mb, browser_memory_mb]` — so a bad sample (a browser
still starting, a page not yet loaded) can never let the target overshoot
what the old constant allowed.

A browser's renderer processes are its children, so a subtree is one browser:
the root processes are counted, the whole tree's RSS is summed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Process names a browser launched by this project runs under. Camoufox is a
# Firefox build; Botasaurus drives Chrome/Chromium.
_BROWSER_NAMES = ("firefox", "camoufox", "chrome", "chromium")


@dataclass(frozen=True)
class BrowserMemorySample:
    """`count` live browsers weighing `mean_mb` each, on this process."""

    count: int
    mean_mb: float


def _is_browser(name: str) -> bool:
    lowered = name.lower()
    return any(candidate in lowered for candidate in _BROWSER_NAMES)


def sample_browser_rss() -> BrowserMemorySample | None:
    """Mean RSS (MB) of this process's live browsers, None when there are
    none — or when psutil is unavailable, which leaves the controller on the
    configured constant exactly as before this existed."""
    try:
        import psutil
    except ImportError:  # pragma: no cover - psutil ships with botasaurus
        return None
    try:
        me = psutil.Process()
        descendants = me.children(recursive=True)
    except Exception:
        return None
    pids = {proc.pid for proc in descendants}
    roots = 0
    total_kb = 0.0
    for proc in descendants:
        try:
            if not _is_browser(proc.name()):
                continue
            # A renderer's parent is the browser itself: only the top of each
            # browser's tree counts as a browser, but all of its memory does.
            parent = proc.parent()
            # Read the memory before counting the browser: a process that
            # exits between the two would otherwise count as a browser
            # weighing nothing and drag the host's average down.
            rss_kb = proc.memory_info().rss / 1024
            if parent is not None and parent.pid in pids and _is_browser(parent.name()):
                total_kb += rss_kb
                continue
            roots += 1
            total_kb += rss_kb
        except Exception:
            # A browser that exited mid-walk is not a measurement failure.
            continue
    if roots == 0:
        return None
    return BrowserMemorySample(count=roots, mean_mb=total_kb / 1024 / roots)
