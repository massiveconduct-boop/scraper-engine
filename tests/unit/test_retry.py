# tests/unit/test_retry.py
"""Retry strategy + backoff — spec §7 error handling matrix."""

import pytest

from core.models import FailureCategory
from core.retry import (
    RETRY_MATRIX,
    RetryStrategy,
    backoff_delay,
    retry_with_backoff,
)


class TestRetryMatrix:
    def test_all_categories_covered(self) -> None:
        """Every FailureCategory must have a RetryStrategy in the matrix."""
        for cat in FailureCategory:
            assert cat in RETRY_MATRIX, f"Missing retry strategy for {cat}"

    def test_non_retryable_categories(self) -> None:
        """Categories that must NOT be retried."""
        non_retryable = {
            FailureCategory.SSRF_BLOCKED,
            FailureCategory.QUOTA_EXCEEDED,
            FailureCategory.PROXY_EXHAUSTED,
            FailureCategory.CIRCUIT_OPEN,
            FailureCategory.CAPTCHA_TRIGGERED,
            FailureCategory.DETECTION_BLOCK,
        }
        for cat in non_retryable:
            assert RETRY_MATRIX[cat].retryable is False, f"{cat} should not be retryable"

    def test_retryable_categories(self) -> None:
        """Categories that CAN be retried."""
        retryable = {
            FailureCategory.NETWORK_TIMEOUT,
            FailureCategory.PARSE_ERROR,
            FailureCategory.BROWSER_CRASH,
        }
        for cat in retryable:
            assert RETRY_MATRIX[cat].retryable is True, f"{cat} should be retryable"


class TestBackoffDelay:
    def test_exponential_growth(self) -> None:
        strategy = RetryStrategy(
            max_attempts=3, base_delay_seconds=2.0, max_delay_seconds=60.0
        )
        d0 = backoff_delay(strategy, 0)  # ~2s
        d1 = backoff_delay(strategy, 1)  # ~4s
        d2 = backoff_delay(strategy, 2)  # ~8s
        assert d0 < d1 < d2

    def test_max_capped(self) -> None:
        strategy = RetryStrategy(
            max_attempts=10, base_delay_seconds=20.0, max_delay_seconds=30.0
        )
        for attempt in range(10):
            delay = backoff_delay(strategy, attempt)
            assert delay <= 30.0 * 1.5  # jitter can add up to 50%

    def test_jitter_disabled(self) -> None:
        strategy = RetryStrategy(
            max_attempts=3, base_delay_seconds=2.0, max_delay_seconds=60.0, jitter=False
        )
        delay = backoff_delay(strategy, 0)
        assert delay == 2.0  # exact, no jitter


class TestRetryWithBackoff:
    @pytest.mark.asyncio
    async def test_returns_on_success(self) -> None:
        call_count = 0

        async def fn():
            nonlocal call_count
            call_count += 1
            return "ok"

        result = await retry_with_backoff(fn, FailureCategory.NETWORK_TIMEOUT)
        assert result == "ok"
        assert call_count == 1

    @pytest.mark.asyncio
    async def test_retries_and_succeeds(self) -> None:
        call_count = 0

        async def fn():
            nonlocal call_count
            call_count += 1
            if call_count < 2:
                raise ConnectionError("fail")
            return "ok"

        result = await retry_with_backoff(fn, FailureCategory.NETWORK_TIMEOUT)
        assert result == "ok"
        assert call_count == 2

    @pytest.mark.asyncio
    async def test_exhausts_retries(self) -> None:
        async def fn():
            raise ConnectionError("always fail")

        with pytest.raises(ConnectionError):
            await retry_with_backoff(fn, FailureCategory.NETWORK_TIMEOUT)

    @pytest.mark.asyncio
    async def test_no_retry_for_non_retryable(self) -> None:
        call_count = 0

        async def fn():
            nonlocal call_count
            call_count += 1
            raise ConnectionError("fail")

        with pytest.raises(ConnectionError):
            await retry_with_backoff(fn, FailureCategory.SSRF_BLOCKED)
        assert call_count == 1  # no retry attempted
