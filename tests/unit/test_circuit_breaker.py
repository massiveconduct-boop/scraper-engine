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
    async def test_success_while_closed_counts_as_attempt_without_resetting_failures(
        self, breaker, redis
    ) -> None:
        """Round 61 — fixes the round-43 open thread: failure_threshold's
        ratio was vestigial because failure_window_attempts used to reset
        to 0 on any success, forcing failures==attempts (rate always 1.0)
        whenever the trip check ran. Now a closed-state success is a real
        attempt: it increments failure_window_attempts (grows the ratio's
        denominator) but must NOT zero failure_window_failures — otherwise
        the ratio is still fake, just reset one call later instead of on
        the spot."""
        await redis.set("cb:mixed2.com:failure_window_attempts", "7")
        await redis.set("cb:mixed2.com:failure_window_failures", "7")
        await breaker.record_success("mixed2.com")
        assert await redis.get("cb:mixed2.com:failure_window_attempts") == "8"
        assert await redis.get("cb:mixed2.com:failure_window_failures") == "7"
        assert await breaker.state("mixed2.com") == CircuitState.CLOSED

    @pytest.mark.asyncio
    async def test_success_while_half_open_closes_circuit(self, breaker, redis) -> None:
        await redis.set("cb:recover.com:state", CircuitState.HALF_OPEN.value)
        await breaker.record_success("recover.com")
        assert await breaker.state("recover.com") == CircuitState.CLOSED
        assert await redis.get("cb:recover.com:failure_window_failures") == "0"
        assert await redis.get("cb:recover.com:failure_window_attempts") == "0"


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
        below failure_threshold (0.5) — must not trip, and (round 61) the
        window resets after being fully sampled so a stale ratio doesn't
        linger and dilute the next batch."""
        await redis.set("cb:mixed.com:failure_window_attempts", "9")
        await redis.set("cb:mixed.com:failure_window_failures", "3")
        await breaker.record_failure("mixed.com")  # -> attempts=10, failures=4
        assert await breaker.state("mixed.com") == CircuitState.CLOSED
        assert await redis.get("cb:mixed.com:failure_window_attempts") == "0"
        assert await redis.get("cb:mixed.com:failure_window_failures") == "0"

    @pytest.mark.asyncio
    async def test_failure_rate_trips_despite_one_earlier_success(self, breaker) -> None:
        """Round 61 — the actual fix under test. One success followed by 9
        failures (10 attempts, 9 failures, rate 0.9 >= failure_threshold
        0.5) must trip. Under the pre-fix code this was impossible without
        directly seeding Redis: record_success zeroed both counters, so
        reaching attempt_threshold again required 10 fresh consecutive
        failures after the success — a 9-failure run like this one never
        tripped, regardless of failure_threshold's configured value."""
        await breaker.record_success("almosttrip.com")
        for _ in range(9):
            await breaker.record_failure("almosttrip.com")
        assert await breaker.state("almosttrip.com") == CircuitState.OPEN

    @pytest.mark.asyncio
    async def test_failure_rate_at_threshold_opens_circuit(self, breaker) -> None:
        for _ in range(10):
            await breaker.record_failure("blown.com")
        assert await breaker.state("blown.com") == CircuitState.OPEN


class TestFailureStreakTtl:
    """Round 43 — live-caught: consecutive_failures/failure_window_attempts
    had no TTL, so failures from one job (a crashed run, a hard-killed
    timeout) sat in Redis forever and silently fed an unrelated LATER job's
    trip decision — confirmed in production Redis state where domains
    showed trip_count/consecutive_failures far too high for the single
    batch that reported them as circuit_open. Both streak keys must expire
    after a quiet period so only recent failures count."""

    @pytest.mark.asyncio
    async def test_record_failure_sets_ttl_on_streak_keys(self, breaker, redis) -> None:
        await breaker.record_failure("ttl.com")
        assert await redis.ttl("cb:ttl.com:failure_window_attempts") > 0
        assert await redis.ttl("cb:ttl.com:failure_window_failures") > 0

    @pytest.mark.asyncio
    async def test_stale_failure_streak_expires_independent_of_new_job(
        self, redis
    ) -> None:
        """A short TTL simulates a failure streak going quiet — Redis
        expiring the keys must mean a fresh failure afterward starts a new
        streak from zero, not from wherever the old, stale streak left off."""
        breaker = CircuitBreaker(
            redis=redis,
            failure_threshold=0.5,
            attempt_threshold=10,
            cooldown_seconds=1,
            max_cooldown_seconds=60,
            failure_streak_ttl_seconds=1,
        )
        for _ in range(9):  # one short of attempt_threshold — stays CLOSED
            await breaker.record_failure("staleburst.com")
        assert await breaker.state("staleburst.com") == CircuitState.CLOSED

        import asyncio

        await asyncio.sleep(1.2)  # let the streak TTL expire

        await breaker.record_failure("staleburst.com")
        assert await redis.get("cb:staleburst.com:failure_window_attempts") == "1"
        assert await breaker.state("staleburst.com") == CircuitState.CLOSED


class TestOpenCircuitBackoff:
    @pytest.mark.asyncio
    async def test_trip_count_and_cooldown_recorded(self, breaker, redis) -> None:
        for _ in range(10):
            await breaker.record_failure("tripped.com")
        assert await redis.get("cb:tripped.com:trip_count") == "1"
        assert await redis.get("cb:tripped.com:cooldown_until") is not None
        assert await redis.get("cb:tripped.com:failure_window_attempts") == "0"
        assert await redis.get("cb:tripped.com:failure_window_failures") == "0"

    @pytest.mark.asyncio
    async def test_trip_count_has_ttl_for_decay(self, breaker, redis) -> None:
        """Round 43 — trip_count must expire after a sustained quiet period
        (a multiple of max_cooldown_seconds) so a domain that tripped once
        long ago, then ran healthy for a long time, doesn't get hit with
        compounded exponential backoff on its next trip as if the earlier
        trip were recent."""
        for _ in range(10):
            await breaker.record_failure("decaying.com")
        assert await redis.ttl("cb:decaying.com:trip_count") > 0

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
