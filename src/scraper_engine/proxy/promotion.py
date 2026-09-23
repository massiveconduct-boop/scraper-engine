# proxy/promotion.py
"""Background proxy promotion — re-validates TCP-only proxies to validated tier.

Per plan §4.2: bounded batch size, bounded concurrency, hard 5-attempt cap
per proxy. Exhausted proxies remain in the pool at score < 40 for auditing
but never get re-validated again. Runs in the same process as the harvester
daemon — not a separate container.

Critical constraint (§4.2): do NOT turn this into a second background
process hammering dead IPs forever. ~0.02% HTTP-forwarding success rate
on free proxies means unbounded retries are self-DOS.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Coroutine
from typing import TYPE_CHECKING

from scraper_engine.proxy.harvester import _score_validation, _to_asn_class
from scraper_engine.proxy.scoring import compute_success_rate

if TYPE_CHECKING:
    import asyncpg

    from scraper_engine.core.models import AnonymityLevel
    from scraper_engine.core.tenant import TenantId
    from scraper_engine.proxy.harvester import SupportsClassify
    from scraper_engine.storage.postgres_client import PostgresClient

logger = logging.getLogger(__name__)

MAX_PROMOTION_ATTEMPTS = 5
PROMOTION_BATCH_SIZE = 20  # don't re-validate entire tcp-only tier at once
PROMOTION_CONCURRENCY = 5  # bounded parallel HTTP validations
PROMOTION_COOLDOWN_SECONDS = 900  # 15 minutes between attempts per proxy

ValidateFn = Callable[
    # timeout is an impl detail with a default on the concrete function, so the
    # contract is the 3 required args; a fn with an extra defaulted param still
    # satisfies this.
    [str, int, str],
    Coroutine[None, None, tuple[bool, "AnonymityLevel", "int | None"]],
]


class ProxyPromotionJob:
    """Re-validate low-score proxies with attempt tracking and bounds.

    Uses the harvester's existing _http_validate function — no duplication.
    Injected via constructor to avoid circular imports.
    """

    def __init__(
        self,
        pg: PostgresClient,
        http_validate_fn: ValidateFn,
        system_tenant: TenantId | None = None,
        asn_classifier: SupportsClassify | None = None,
    ) -> None:
        from scraper_engine.core.tenant import TenantId
        from scraper_engine.proxy.asn_classifier import NullAsnClassifier

        self._pg = pg
        self._http_validate = http_validate_fn
        self._tenant: TenantId = system_tenant or TenantId("system")
        self._sem = asyncio.Semaphore(PROMOTION_CONCURRENCY)
        self._classifier: SupportsClassify = asn_classifier or NullAsnClassifier()

    async def run_once(self) -> dict[str, int]:
        """Execute one promotion cycle. Returns counts keyed by outcome."""
        async with self._pg.acquire(self._tenant) as conn:
            candidates = await conn.fetch(
                """SELECT id, ip, port, protocol, promotion_attempts,
                          global_success_count, global_failure_count
                   FROM proxy_pool
                   WHERE reliability_score < 40
                     AND promotion_attempts < $1
                     AND (last_promotion_attempt_at IS NULL
                          OR last_promotion_attempt_at < NOW() - ($2 || ' seconds')::INTERVAL)
                   ORDER BY last_promotion_attempt_at ASC NULLS FIRST
                   LIMIT $3""",
                MAX_PROMOTION_ATTEMPTS,
                str(PROMOTION_COOLDOWN_SECONDS),
                PROMOTION_BATCH_SIZE,
            )

        promoted = 0
        failed = 0
        exhausted = 0

        async def _try_one(row: asyncpg.Record) -> None:
            nonlocal promoted, failed, exhausted
            async with self._sem:
                is_valid, anonymity, latency_ms = await self._http_validate(
                    row["ip"],
                    row["port"],
                    row["protocol"],
                )
            async with self._pg.acquire(self._tenant) as conn:
                new_attempts = row["promotion_attempts"] + 1
                if is_valid:
                    asn = _to_asn_class(await self._classifier.classify(row["ip"]))
                    success_rate = compute_success_rate(
                        row["global_success_count"], row["global_failure_count"]
                    )
                    score = _score_validation(latency_ms, anonymity, asn, success_rate)
                    await conn.execute(
                        """UPDATE proxy_pool
                           SET reliability_score = $1,
                               anonymity_level = $2,
                               asn_class = $3,
                               response_time_ms = $4,
                               promotion_attempts = promotion_attempts + 1,
                               last_promotion_attempt_at = NOW()
                           WHERE id = $5""",
                        score,
                        anonymity.value,
                        asn.value,
                        latency_ms,
                        row["id"],
                    )
                    promoted += 1
                else:
                    await conn.execute(
                        """UPDATE proxy_pool
                           SET promotion_attempts = promotion_attempts + 1,
                               last_promotion_attempt_at = NOW()
                           WHERE id = $1""",
                        row["id"],
                    )
                    failed += 1
                    # A count, not an `if`: an `if` as the last statement of an
                    # `async with` body records its "false" exit differently on
                    # Python 3.11, so 3.11's branch coverage reported that path
                    # missed although tests take it (round 67, PR #31 CI).
                    exhausted += int(new_attempts >= MAX_PROMOTION_ATTEMPTS)

        if candidates:
            await asyncio.gather(*[_try_one(row) for row in candidates])

        logger.info(
            "promotion cycle: %d candidates, %d promoted, %d failed, %d exhausted",
            len(candidates),
            promoted,
            failed,
            exhausted,
        )
        return {
            "candidates": len(candidates),
            "promoted": promoted,
            "failed": failed,
            "exhausted": exhausted,
        }

    async def run_forever(self, interval_seconds: int = 900) -> None:
        """Run promotion cycles indefinitely with interval_seconds between cycles."""
        while True:
            try:
                await self.run_once()
            except Exception:
                logger.exception("promotion cycle failed, will retry next interval")
            await asyncio.sleep(interval_seconds)
