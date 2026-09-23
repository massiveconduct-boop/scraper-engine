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


# Round 43 — see _open_circuit's trip_count TTL comment.
_TRIP_COUNT_DECAY_MULTIPLIER = 3


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
        failure_streak_ttl_seconds: int = 600,
    ) -> None:
        self._redis = redis
        self._failure_threshold = failure_threshold
        self._attempt_threshold = attempt_threshold
        self._cooldown_seconds = cooldown_seconds
        self._max_cooldown_seconds = max_cooldown_seconds
        # Round 43 — live-caught: consecutive_failures/failure_window_attempts
        # are never TTL'd, so a burst of failures from one job (a crashed run,
        # a hard-killed timeout) sits in Redis indefinitely and silently feeds
        # into a completely unrelated LATER job's trip decision — a domain can
        # get circuit-tripped by failures from hours-old, already-abandoned
        # attempts that have nothing to do with its current health. Expiring
        # the streak counters after a quiet period means only a genuinely
        # *recent* run of failures can trip the circuit.
        self._failure_streak_ttl_seconds = failure_streak_ttl_seconds

    def _key(self, domain: str, suffix: str) -> str:
        return f"cb:{domain}:{suffix}"

    async def _get(self, key: str) -> str | None:
        result = await self._redis.get(key)
        return str(result) if result else None

    async def _set(self, key: str, value: str, ttl_seconds: int | None = None) -> None:
        if ttl_seconds is not None:
            await self._redis.set(key, value, ex=ttl_seconds)
        else:
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

    async def _reset_window(self, domain: str) -> None:
        await self._set(self._key(domain, "failure_window_attempts"), "0")
        await self._set(self._key(domain, "failure_window_failures"), "0")

    async def _record_attempt(self, domain: str, *, failed: bool) -> tuple[int, int]:
        """Increment the rolling window and return (attempts, failures).

        Round 61 fix — `failure_window_attempts` used to be reset to 0 on
        every success (see record_success's old docstring below), so by the
        time attempt_threshold triggered a trip check, failures always
        equaled attempts and failure_rate was always 1.0: attempt_threshold
        alone decided trips, and failure_threshold was a dead comparison
        (round 43 open thread, never fixed until now). Fix: attempts now
        increments on every call — success or failure — while failures only
        increments on a failure, and neither resets on a lone success. That
        makes failure_threshold real: e.g. attempt_threshold=20,
        failure_threshold=0.95 now genuinely tolerates up to 1 success in
        the last 20 attempts before tripping, instead of requiring 20
        purely-consecutive failures with zero successes mixed in.
        """
        attempts_raw = await self._get(self._key(domain, "failure_window_attempts"))
        failures_raw = await self._get(self._key(domain, "failure_window_failures"))

        attempts = (int(attempts_raw) if attempts_raw else 0) + 1
        failures = (int(failures_raw) if failures_raw else 0) + (1 if failed else 0)

        await self._set(
            self._key(domain, "failure_window_attempts"),
            str(attempts),
            ttl_seconds=self._failure_streak_ttl_seconds,
        )
        await self._set(
            self._key(domain, "failure_window_failures"),
            str(failures),
            ttl_seconds=self._failure_streak_ttl_seconds,
        )
        return attempts, failures

    async def record_success(self, domain: str) -> None:
        """Record a successful request. Closes circuit if half-open.

        A half-open success (recovery confirmed) fully resets the window.
        An ordinary closed-state success still counts as a real attempt in
        the rolling window (round 61 — see _record_attempt); the window
        itself resets once attempt_threshold attempts have been sampled
        without tripping, so a long healthy run doesn't dilute the ratio
        forever.
        """
        current = await self.state(domain)
        if current == CircuitState.HALF_OPEN:
            await self._set(self._key(domain, "state"), CircuitState.CLOSED.value)
            await self._reset_window(domain)
            return

        attempts, _ = await self._record_attempt(domain, failed=False)
        if attempts >= self._attempt_threshold:
            await self._reset_window(domain)

    async def record_failure(self, domain: str) -> None:
        """Record a failed request. May open circuit if threshold exceeded."""
        current = await self.state(domain)

        if current == CircuitState.HALF_OPEN:
            await self._open_circuit(domain)
            return

        attempts, failures = await self._record_attempt(domain, failed=True)

        if attempts >= self._attempt_threshold:
            failure_rate = failures / attempts
            if failure_rate >= self._failure_threshold:
                await self._open_circuit(domain)
            else:
                await self._reset_window(domain)

    async def _open_circuit(self, domain: str) -> None:
        """Open the circuit with exponential backoff cooldown."""
        trip_raw = await self._get(self._key(domain, "trip_count"))
        trip_count = (int(trip_raw) if trip_raw else 0) + 1
        # Round 43 — trip_count TTL'd so it decays after a sustained quiet
        # period, instead of compounding forever. Before this, a domain that
        # tripped a handful of times, then ran healthy for days/weeks, still
        # got hit with the FULL exponential backoff on its next trip — the
        # counter had no memory of "that was a long time ago." TTL window is
        # a multiple of max_cooldown_seconds so it only decays once a domain
        # has genuinely been quiet well past its own longest possible
        # cooldown, not mid-cycle while it's still actively recovering.
        await self._set(
            self._key(domain, "trip_count"),
            str(trip_count),
            ttl_seconds=self._max_cooldown_seconds * _TRIP_COUNT_DECAY_MULTIPLIER,
        )
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
        await self._reset_window(domain)
