# core/host_identity.py
"""Which physical host this process runs on (round 65).

orchestrator/host_capacity.py keeps one browser budget per HOST, shared by every
worker container on it, so each process needs a name for the machine it is
on that every sibling container agrees on. A container's hostname does not
work (it is the container id). The kernel's boot_id does: containers share
the host kernel, so they all read the same value — verified live on this
deployment (host and worker-l1 both read the same boot_id). A reboot changes
it, which is harmless because every host-namespaced key carries a TTL.

`SCRAPER_HOST_ID` overrides it, for runtimes where containers do NOT share
the host kernel (VM-isolated runtimes such as Kata or Docker Desktop see
their VM's boot_id instead).
"""

from __future__ import annotations

import os
from pathlib import Path

_BOOT_ID_PATH = Path("/proc/sys/kernel/random/boot_id")
_FALLBACK_HOST_ID = "local"


def resolve_host_id(boot_id_path: Path = _BOOT_ID_PATH) -> str:
    """`SCRAPER_HOST_ID`, else the kernel boot_id, else "local".

    The last fallback only applies where /proc is unavailable (not Linux);
    every process on such a machine still agrees on it.
    """
    override = os.environ.get("SCRAPER_HOST_ID", "").strip()
    if override:
        return override
    try:
        boot_id = boot_id_path.read_text().strip()
    except OSError:
        return _FALLBACK_HOST_ID
    return boot_id or _FALLBACK_HOST_ID
