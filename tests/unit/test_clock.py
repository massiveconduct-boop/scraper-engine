# tests/unit/test_clock.py
"""Clock tests — injectable time source for testability."""

from datetime import UTC, datetime

from core.clock import FrozenClock, SystemClock


class TestSystemClock:
    def test_now_returns_datetime(self) -> None:
        clock = SystemClock()
        now = clock.now()
        assert isinstance(now, datetime)
        assert now.tzinfo is not None

    def test_timestamp_ms(self) -> None:
        clock = SystemClock()
        ts = clock.timestamp_ms()
        assert isinstance(ts, int)
        assert ts > 1_700_000_000_000  # July 2023+


class TestFrozenClock:
    def test_default_frozen_at(self) -> None:
        clock = FrozenClock()
        now = clock.now()
        assert now.year == 2026
        assert now.month == 1
        assert now.day == 1

    def test_custom_frozen_at(self) -> None:
        dt = datetime(2025, 6, 15, tzinfo=UTC)
        clock = FrozenClock(frozen_at=dt)
        assert clock.now() == dt

    def test_advance(self) -> None:
        clock = FrozenClock()
        clock.advance(3600)
        assert clock.now().hour == 1

    def test_timestamp_ms_consistent(self) -> None:
        clock = FrozenClock()
        ts1 = clock.timestamp_ms()
        ts2 = clock.timestamp_ms()
        assert ts1 == ts2  # frozen time

    def test_advance_affects_timestamp(self) -> None:
        clock = FrozenClock()
        ts1 = clock.timestamp_ms()
        clock.advance(1.0)
        ts2 = clock.timestamp_ms()
        assert ts2 > ts1
