# tests/unit/test_circuit_breaker.py
"""CircuitBreaker state machine (orchestrator/circuit_breaker.py).

Uses fakeredis (in-memory, no network I/O) rather than mocking the Redis
calls away — the thing under test is the state transition logic that reads
back its own writes, so a mock returning canned values wouldn't exercise
real behavior for most of it. Mirrors tests/integration/test_circuit_breaker.py's
fixture shape; this file exists so `pytest tests/unit/` alone (the fast/CI
path) covers this module without requiring the integration suite.
"""

import pytest
from fakeredis import FakeAsyncRedis

from scraper_engine.orchestrator.circuit_breaker import CircuitBreaker, CircuitState


@pytest.fixture
async def redis():
    return FakeAsyncRedis(decode_responses=True)


@pytest.fixture
async def breaker(redis):
    return CircuitBreaker(
        redis=redis,
        failure_threshold=0.5,
        attempt_threshold=10,
        cooldown_seconds=1,
        max_cooldown_seconds=60,
    )


class TestState:
    @pytest.mark.asyncio
    async def test_no_state_key_defaults_closed(self, breaker) -> None:
        assert await breaker.state("fresh.com") == CircuitState.CLOSED


class TestAllowRequest:
    @pytest.mark.asyncio
    async def test_closed_allows(self, breaker) -> None:
        assert await breaker.allow_request("example.com") is True

    @pytest.mark.asyncio
    async def test_open_without_cooldown_key_denies(self, breaker, redis) -> None:
        """OPEN state but no cooldown_until key recorded (e.g. a partial
        write) — must fail closed rather than treat missing as expired."""
        await redis.set("cb:nokey.com:state", CircuitState.OPEN.value)
        assert await breaker.allow_request("nokey.com") is False

    @pytest.mark.asyncio
    async def test_open_before_cooldown_expiry_denies(self, breaker) -> None:
        for _ in range(10):
            await breaker.record_failure("stillopen.com")
        assert await breaker.allow_request("stillopen.com") is False

    @pytest.mark.asyncio
    async def test_open_after_cooldown_expiry_transitions_to_half_open(
        self, breaker, redis
    ) -> None:
        for _ in range(10):
            await breaker.record_failure("expired.com")
        await redis.set("cb:expired.com:cooldown_until", "0")
        assert await breaker.allow_request("expired.com") is True
        assert await breaker.state("expired.com") == CircuitState.HALF_OPEN

    @pytest.mark.asyncio
    async def test_half_open_allows_probe(self, breaker, redis) -> None:
        await redis.set("cb:probing.com:state", CircuitState.HALF_OPEN.value)
        assert await breaker.allow_request("probing.com") is True


class TestRecordSuccess:
    @pytest.mark.asyncio
    async def test_success_while_closed_resets_window_only(self, breaker, redis) -> None:
        await breaker.record_success("closedok.com")
        assert await redis.get("cb:closedok.com:failure_window_attempts") == "0"
        assert await breaker.state("closedok.com") == CircuitState.CLOSED

    @pytest.mark.asyncio
    async def test_success_while_half_open_closes_circuit(self, breaker, redis) -> None:
        await redis.set("cb:recover.com:state", CircuitState.HALF_OPEN.value)
        await breaker.record_success("recover.com")
        assert await breaker.state("recover.com") == CircuitState.CLOSED
        assert await redis.get("cb:recover.com:consecutive_failures") == "0"


class TestRecordFailure:
    @pytest.mark.asyncio
    async def test_failure_while_half_open_reopens_immediately(self, breaker, redis) -> None:
        await redis.set("cb:doomed.com:state", CircuitState.HALF_OPEN.value)
        await breaker.record_failure("doomed.com")
        assert await breaker.state("doomed.com") == CircuitState.OPEN

    @pytest.mark.asyncio
    async def test_failure_below_attempt_threshold_stays_closed(self, breaker) -> None:
        for _ in range(9):  # attempt_threshold=10
            await breaker.record_failure("almost.com")
        assert await breaker.state("almost.com") == CircuitState.CLOSED

    @pytest.mark.asyncio
    async def test_failure_rate_below_threshold_stays_closed(self, breaker, redis) -> None:
        """Reaches attempt_threshold (10) but failure_rate (4/10=0.4) stays
        below failure_threshold (0.5) — must not trip."""
        await redis.set("cb:mixed.com:failure_window_attempts", "9")
        await redis.set("cb:mixed.com:consecutive_failures", "3")
        await breaker.record_failure("mixed.com")  # -> attempts=10, failures=4
        assert await breaker.state("mixed.com") == CircuitState.CLOSED

    @pytest.mark.asyncio
    async def test_failure_rate_at_threshold_opens_circuit(self, breaker) -> None:
        for _ in range(10):
            await breaker.record_failure("blown.com")
        assert await breaker.state("blown.com") == CircuitState.OPEN


class TestOpenCircuitBackoff:
    @pytest.mark.asyncio
    async def test_trip_count_and_cooldown_recorded(self, breaker, redis) -> None:
        for _ in range(10):
            await breaker.record_failure("tripped.com")
        assert await redis.get("cb:tripped.com:trip_count") == "1"
        assert await redis.get("cb:tripped.com:cooldown_until") is not None
        assert await redis.get("cb:tripped.com:failure_window_attempts") == "0"

    @pytest.mark.asyncio
    async def test_repeated_trips_double_cooldown(self, breaker, redis) -> None:
        for _ in range(10):
            await breaker.record_failure("repeat.com")
        first_cooldown = float(await redis.get("cb:repeat.com:cooldown_until"))

        await redis.set("cb:repeat.com:cooldown_until", "0")
        await breaker.allow_request("repeat.com")  # -> half_open
        await breaker.record_failure("repeat.com")  # re-open, trip_count=2

        trip2 = await redis.get("cb:repeat.com:trip_count")
        assert trip2 == "2"
        second_cooldown = float(await redis.get("cb:repeat.com:cooldown_until"))
        assert second_cooldown > first_cooldown

    @pytest.mark.asyncio
    async def test_cooldown_capped_at_max(self, redis) -> None:
        breaker = CircuitBreaker(
            redis=redis,
            failure_threshold=0.5,
            attempt_threshold=1,
            cooldown_seconds=100,
            max_cooldown_seconds=150,
        )
        await redis.set("cb:capped.com:trip_count", "10")  # would blow past max uncapped
        await breaker.record_failure("capped.com")
        cooldown_until = float(await redis.get("cb:capped.com:cooldown_until"))
        import time

        assert cooldown_until <= time.time() + 150 + 1

    @pytest.mark.asyncio
    async def test_open_circuit_increments_global_metric(self, breaker, redis) -> None:
        for _ in range(10):
            await breaker.record_failure("metrictest.com")
        assert await redis.get("metrics:circuit_breaker_trips_total") == "1"
