# tests/unit/test_startup.py
"""core/startup.py::wait_for_dependency — round 62.

The behavior under test is "does not give up", which is awkward to assert
directly. Every test here therefore either uses max_attempts (the
test-only escape hatch) or a connect() that is guaranteed to succeed on a
known attempt, and asserts on the CALL COUNT — the number of times the
process was willing to try — rather than on wall-clock waiting.

asyncio.sleep is monkeypatched out in all of them: the real backoff climbs
to a 30s ceiling, and the point of the assertions is the retry arithmetic,
not the delays themselves. The delays that were actually requested are
captured and asserted on separately.
"""

import logging

import pytest

from scraper_engine.core import startup
from scraper_engine.core.startup import wait_for_dependency


@pytest.fixture
def slept(monkeypatch):
    """Collects every requested sleep duration instead of waiting."""
    recorded: list[float] = []

    async def _fake_sleep(seconds: float) -> None:
        recorded.append(seconds)

    monkeypatch.setattr(startup.asyncio, "sleep", _fake_sleep)
    return recorded


class TestWaitForDependency:
    @pytest.mark.asyncio
    async def test_returns_immediately_when_connect_succeeds(self, slept):
        calls = []

        async def _connect():
            calls.append(1)
            return "client"

        assert await wait_for_dependency("postgres", _connect) == "client"
        assert len(calls) == 1
        assert slept == []

    @pytest.mark.asyncio
    async def test_retries_until_connect_succeeds(self, slept):
        """The outage case. Three refused connections in a row must not end
        the process — the fourth attempt is the one that matters, and before
        round 62 it never happened."""
        calls = []

        async def _connect():
            calls.append(1)
            if len(calls) < 4:
                raise ConnectionRefusedError("connection refused")
            return "client"

        assert await wait_for_dependency("postgres", _connect) == "client"
        assert len(calls) == 4
        assert len(slept) == 3

    @pytest.mark.asyncio
    async def test_backoff_is_exponential_and_capped(self, slept):
        calls = []

        async def _connect():
            calls.append(1)
            if len(calls) < 10:
                raise ConnectionRefusedError("connection refused")
            return "client"

        await wait_for_dependency("minio", _connect)

        assert slept[:4] == [1.0, 2.0, 4.0, 8.0]  # BASE 0.5 * 2**attempt
        assert max(slept) == startup._MAX_DELAY_SECONDS
        assert all(d <= startup._MAX_DELAY_SECONDS for d in slept)

    @pytest.mark.asyncio
    async def test_max_attempts_exhausted_reraises_last_error(self, slept):
        calls = []

        async def _connect():
            calls.append(1)
            raise ConnectionRefusedError("connection refused")

        with pytest.raises(ConnectionRefusedError):
            await wait_for_dependency("redis", _connect, max_attempts=3)

        assert len(calls) == 3
        # No sleep after the final attempt — nothing is coming after it.
        assert len(slept) == 2

    @pytest.mark.asyncio
    async def test_logs_escalate_from_warning_to_error(self, slept, caplog):
        """Below the threshold this is an ordinary start-order race and must
        not page anyone; past it, a real misconfiguration is the likelier
        explanation and has to be visible at ERROR even though the process
        keeps waiting either way."""
        calls = []

        async def _connect():
            calls.append(1)
            if len(calls) <= startup._ESCALATE_AFTER_ATTEMPTS:
                raise ConnectionRefusedError("connection refused")
            return "client"

        with caplog.at_level(logging.WARNING, logger=startup.logger.name):
            await wait_for_dependency("pgbouncer", _connect)

        levels = [r.levelno for r in caplog.records if r.message.startswith("startup dependency")]
        assert levels.count(logging.WARNING) == startup._ESCALATE_AFTER_ATTEMPTS - 1
        assert levels.count(logging.ERROR) == 1

    @pytest.mark.asyncio
    async def test_logs_recovery_only_after_a_real_retry(self, slept, caplog):
        async def _ok():
            return "client"

        with caplog.at_level(logging.INFO, logger=startup.logger.name):
            await wait_for_dependency("redis", _ok)

        assert not [r for r in caplog.records if "connected after retries" in r.message]
