# core/clock.py
from __future__ import annotations

from datetime import UTC, datetime
from typing import Protocol


class Clock(Protocol):
    """Injectable time source for testability."""

    def now(self) -> datetime:
        ...

    def timestamp_ms(self) -> int:
        ...


class SystemClock:
    """Production clock using real system time."""

    def now(self) -> datetime:
        return datetime.now(UTC)

    def timestamp_ms(self) -> int:
        return int(datetime.now(UTC).timestamp() * 1000)


class FrozenClock:
    """Test clock that returns a fixed time, advanceable manually."""

    def __init__(self, frozen_at: datetime | None = None) -> None:
        self._now = frozen_at or datetime(2026, 1, 1, tzinfo=UTC)

    def now(self) -> datetime:
        return self._now

    def timestamp_ms(self) -> int:
        return int(self._now.timestamp() * 1000)

    def advance(self, seconds: float) -> None:
        from datetime import timedelta

        self._now += timedelta(seconds=seconds)
