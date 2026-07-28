# orchestrator/circuit_breaker.py
"""3-state circuit breaker (closed/open/half-open) with exponential backoff.

Closes F-18: 3 states with exponential backoff across repeated trips
prevents the thundering-herd re-attack on recovery.
"""

from __future__ import annotations

import time
from enum import Enum
from typing import Any


class CircuitState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreaker:
    """3-state circuit breaker per domain, backed by Redis.

    CLOSED: normal operation, requests flow through.
    OPEN: failures exceeded threshold, all requests rejected immediately.
    HALF_OPEN: cooldown expired, probing with a single request.

    Exponential backoff on repeated trips: cooldown doubles each time the
    circuit re-opens, up to max_cooldown_seconds.
    """

    def __init__(
        self,
        redis: Any,
        failure_threshold: float = 0.95,
        attempt_threshold: int = 20,
        cooldown_seconds: int = 600,
        max_cooldown_seconds: int = 3600,
    ) -> None:
        self._redis = redis
        self._failure_threshold = failure_threshold
        self._attempt_threshold = attempt_threshold
        self._cooldown_seconds = cooldown_seconds
        self._max_cooldown_seconds = max_cooldown_seconds

    def _key(self, domain: str, suffix: str) -> str:
        return f"cb:{domain}:{suffix}"

    async def _get(self, key: str) -> str | None:
        result = await self._redis.get(key)
        return str(result) if result else None

    async def _set(self, key: str, value: str) -> None:
        await self._redis.set(key, value)

    async def state(self, domain: str) -> CircuitState:
        """Return the current circuit state for a domain."""
        state_raw = await self._get(self._key(domain, "state"))
        if state_raw is None:
            return CircuitState.CLOSED
        return CircuitState(state_raw)

    async def allow_request(self, domain: str) -> bool:
        """Check if a request should be allowed.

        CLOSED → always True.
        OPEN → False until cooldown elapses, then → HALF_OPEN.
        HALF_OPEN → True (probing), close on success, re-open on failure.
        """
        current = await self.state(domain)

        if current == CircuitState.CLOSED:
            return True

        if current == CircuitState.OPEN:
            cooldown_raw = await self._get(self._key(domain, "cooldown_until"))
            if cooldown_raw is None:
                return False
            cooldown_until = float(cooldown_raw)
            if time.time() < cooldown_until:
                return False
            await self._set(self._key(domain, "state"), CircuitState.HALF_OPEN.value)
            return True

        return True

    async def record_success(self, domain: str) -> None:
        """Record a successful request. Closes circuit if half-open."""
        current = await self.state(domain)
        if current == CircuitState.HALF_OPEN:
            await self._set(self._key(domain, "state"), CircuitState.CLOSED.value)
            await self._set(self._key(domain, "consecutive_failures"), "0")
            await self._set(self._key(domain, "failure_window_attempts"), "0")

        await self._set(self._key(domain, "failure_window_attempts"), "0")

    async def record_failure(self, domain: str) -> None:
        """Record a failed request. May open circuit if threshold exceeded."""
        current = await self.state(domain)

        if current == CircuitState.HALF_OPEN:
            await self._open_circuit(domain)
            return

        attempts_raw = await self._get(self._key(domain, "failure_window_attempts"))
        failures_raw = await self._get(self._key(domain, "consecutive_failures"))

        attempts = (int(attempts_raw) if attempts_raw else 0) + 1
        failures = (int(failures_raw) if failures_raw else 0) + 1

        await self._set(self._key(domain, "failure_window_attempts"), str(attempts))
        await self._set(self._key(domain, "consecutive_failures"), str(failures))

        if attempts >= self._attempt_threshold:
            failure_rate = failures / attempts
            if failure_rate >= self._failure_threshold:
                await self._open_circuit(domain)

    async def _open_circuit(self, domain: str) -> None:
        """Open the circuit with exponential backoff cooldown."""
        trip_raw = await self._get(self._key(domain, "trip_count"))
        trip_count = (int(trip_raw) if trip_raw else 0) + 1
        await self._set(self._key(domain, "trip_count"), str(trip_count))
        # Global counter, not per-domain (round 25) — Redis has no cheap way to
        # enumerate every domain this breaker has ever seen, so a per-domain
        # scrape-time gauge isn't feasible. observability/metrics.py refreshes
        # this into circuit_breaker_trips_total when /metrics is scraped, from
        # the (separate, long-lived) API process.
        await self._redis.incr("metrics:circuit_breaker_trips_total")

        cooldown = min(
            self._cooldown_seconds * (2 ** (trip_count - 1)),
            self._max_cooldown_seconds,
        )
        cooldown_until = time.time() + cooldown

        await self._set(self._key(domain, "state"), CircuitState.OPEN.value)
        await self._set(self._key(domain, "cooldown_until"), str(cooldown_until))
        await self._set(self._key(domain, "failure_window_attempts"), "0")
