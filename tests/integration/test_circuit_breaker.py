# tests/integration/test_circuit_breaker.py
"""Circuit breaker integration tests — 3-state machine with fake Redis."""

import pytest
from fakeredis import FakeAsyncRedis

from scraper_engine.orchestrator.circuit_breaker import CircuitBreaker, CircuitState


@pytest.fixture
async def redis():
    """Create a fake async Redis connection."""
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


class TestCircuitBreaker:
    @pytest.mark.asyncio
    async def test_initial_state_closed(self, breaker):
        state = await breaker.state("example.com")
        assert state == CircuitState.CLOSED

    @pytest.mark.asyncio
    async def test_closed_allows_requests(self, breaker):
        assert await breaker.allow_request("example.com") is True

    @pytest.mark.asyncio
    async def test_opens_after_failures(self, breaker):
        for _ in range(10):
            await breaker.record_failure("test.com")
        state = await breaker.state("test.com")
        assert state == CircuitState.OPEN

    @pytest.mark.asyncio
    async def test_open_rejects_requests(self, breaker):
        for _ in range(10):
            await breaker.record_failure("blocked.com")
        assert await breaker.allow_request("blocked.com") is False

    @pytest.mark.asyncio
    async def test_half_open_after_cooldown(self, breaker, redis):
        for _ in range(10):
            await breaker.record_failure("probe.com")
        # Force cooldown to expire by setting a past timestamp
        await redis.set("cb:probe.com:cooldown_until", "0")
        assert await breaker.allow_request("probe.com") is True
        state = await breaker.state("probe.com")
        assert state == CircuitState.HALF_OPEN

    @pytest.mark.asyncio
    async def test_half_open_success_closes(self, breaker, redis):
        for _ in range(10):
            await breaker.record_failure("recover.com")
        await redis.set("cb:recover.com:cooldown_until", "0")
        await breaker.allow_request("recover.com")
        await breaker.record_success("recover.com")
        state = await breaker.state("recover.com")
        assert state == CircuitState.CLOSED

    @pytest.mark.asyncio
    async def test_half_open_failure_reopens(self, breaker, redis):
        for _ in range(10):
            await breaker.record_failure("doomed.com")
        await redis.set("cb:doomed.com:cooldown_until", "0")
        await breaker.allow_request("doomed.com")
        await breaker.record_failure("doomed.com")
        state = await breaker.state("doomed.com")
        assert state == CircuitState.OPEN

    @pytest.mark.asyncio
    async def test_exponential_backoff(self, breaker, redis):
        for _ in range(10):
            await breaker.record_failure("backoff.com")  # open circuit
        # First trip
        trip1 = await redis.get("cb:backoff.com:trip_count")
        assert trip1 is not None and int(trip1) >= 1

        # Reset and trip again — cooldown should increase
        await redis.set("cb:backoff.com:cooldown_until", "0")
        await breaker.allow_request("backoff.com")
        await breaker.record_failure("backoff.com")  # re-open
        trip2 = await redis.get("cb:backoff.com:trip_count")
        assert int(trip2) > int(trip1), "Trip count should increase on re-open"
