# proxy/gateway_health.py
"""Whether the paid gateway is currently refusing our credentials.

Round 68. A 407 from the gateway is about the ACCOUNT (live: `407
TRAFFIC_EXHAUSTED` once the DataImpulse plan ran out), never the URL, the
level or the site. Before this module every worker learned that again on
every URL: 171 of 171 gateway renders refused over two days in
research_agent's runs, each one a URL sent to the DLQ even though the free
pool that also serves those levels was working.

One refusal now writes a shared Redis key with a TTL
(`dataimpulse.refused_ttl_seconds`). While it exists, orchestrator/worker.py
treats the gateway as unavailable — `free_first` behaves like `free_only`,
`paid_only` fails fast — and `/v1/health` reports it. The key's expiry is
the re-test: the first gateway use after it either succeeds (and clears the
key) or is refused again (and re-writes it). A refused attempt carries no
traffic, so no paid bytes are spent finding out.

The key is account-wide, not per tenant: every tenant goes out through the
same DataImpulse account. Redis failures are swallowed — a missing verdict
costs at most one more refused attempt, never a job.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)

REFUSED_KEY = "paid_gateway:refused"

# `scheme://user:pass@host` -> `scheme://<credentials>@host`. Neither
# Camoufox's nor httpx's 407 message has carried credentials, but this text
# ends up on /v1/health, so it is scrubbed rather than trusted.
_CREDENTIALS_IN_URL = re.compile(r"(?<=://)[^/@\s]+@")
_MAX_ERROR_CHARS = 300


@dataclass(frozen=True)
class GatewayRefusal:
    since: str
    error: str


def _scrub(error: str) -> str:
    return _CREDENTIALS_IN_URL.sub("<credentials>@", error)[:_MAX_ERROR_CHARS]


class GatewayHealth:
    """Reads and writes the shared refusal verdict. Takes the RedisClient,
    not its .raw, and resolves .raw per call — same reason as
    orchestrator/level_memory.py: constructing a Worker must not fail
    before Redis is up."""

    def __init__(self, redis: Any, ttl_seconds: int) -> None:
        self._redis = redis
        self._ttl_seconds = ttl_seconds

    async def refusal(self) -> GatewayRefusal | None:
        try:
            raw = await self._redis.raw.get(REFUSED_KEY)
        except Exception:
            logger.warning("gateway_health_read_failed", exc_info=True)
            return None
        if raw is None:
            return None
        try:
            data = json.loads(raw)
            return GatewayRefusal(since=str(data["since"]), error=str(data["error"]))
        except (ValueError, KeyError, TypeError):
            return GatewayRefusal(since="unknown", error="unreadable refusal record")

    async def mark_refused(self, error: str) -> None:
        record = {"since": datetime.now(UTC).isoformat(), "error": _scrub(error)}
        try:
            # nx: keep the FIRST refusal's timestamp while the outage lasts;
            # a later refusal only needs the key to exist.
            written = await self._redis.raw.set(
                REFUSED_KEY, json.dumps(record), ex=self._ttl_seconds, nx=True
            )
        except Exception:
            logger.warning("gateway_health_write_failed", exc_info=True)
            return
        if written:
            logger.warning(
                "paid_gateway_refused_credentials — gateway out of use for %ss: %s",
                self._ttl_seconds,
                record["error"],
            )

    async def clear(self) -> None:
        try:
            await self._redis.raw.delete(REFUSED_KEY)
        except Exception:
            logger.warning("gateway_health_clear_failed", exc_info=True)
