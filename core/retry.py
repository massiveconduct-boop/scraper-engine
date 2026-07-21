# core/retry.py
from __future__ import annotations

import asyncio
import random
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import TypeVar

from .models import FailureCategory

T = TypeVar("T")


@dataclass(frozen=True)
class RetryStrategy:
    """Category-aware retry/backoff configuration."""

    max_attempts: int
    base_delay_seconds: float
    max_delay_seconds: float
    jitter: bool = True
    retryable: bool = True


# Authoritative retry matrix per failure category (spec §7)
RETRY_MATRIX: dict[FailureCategory, RetryStrategy] = {
    FailureCategory.NETWORK_TIMEOUT: RetryStrategy(
        max_attempts=3, base_delay_seconds=2.0, max_delay_seconds=30.0, retryable=True
    ),
    FailureCategory.DETECTION_BLOCK: RetryStrategy(
        max_attempts=1, base_delay_seconds=0, max_delay_seconds=0, retryable=False
    ),
    FailureCategory.BROWSER_CRASH: RetryStrategy(
        max_attempts=2, base_delay_seconds=5.0, max_delay_seconds=60.0, retryable=True
    ),
    FailureCategory.CAPTCHA_TRIGGERED: RetryStrategy(
        max_attempts=0, base_delay_seconds=0, max_delay_seconds=0, retryable=False
    ),
    FailureCategory.PARSE_ERROR: RetryStrategy(
        max_attempts=2, base_delay_seconds=1.0, max_delay_seconds=10.0, retryable=True
    ),
    FailureCategory.PROXY_EXHAUSTED: RetryStrategy(
        max_attempts=0, base_delay_seconds=0, max_delay_seconds=0, retryable=False
    ),
    FailureCategory.CIRCUIT_OPEN: RetryStrategy(
        max_attempts=0, base_delay_seconds=0, max_delay_seconds=0, retryable=False
    ),
    FailureCategory.SSRF_BLOCKED: RetryStrategy(
        max_attempts=0, base_delay_seconds=0, max_delay_seconds=0, retryable=False
    ),
    FailureCategory.QUOTA_EXCEEDED: RetryStrategy(
        max_attempts=0, base_delay_seconds=0, max_delay_seconds=0, retryable=False
    ),
}


def backoff_delay(strategy: RetryStrategy, attempt: int) -> float:
    """Exponential backoff with optional jitter. Attempt is 0-indexed."""
    delay = min(strategy.base_delay_seconds * (2**attempt), strategy.max_delay_seconds)
    if strategy.jitter:
        delay *= random.uniform(0.5, 1.5)
    return float(delay)


async def retry_with_backoff(
    fn: Callable[[], Coroutine[None, None, T]],
    category: FailureCategory,
    *,
    on_retry: (
        Callable[[FailureCategory, int, Exception], Coroutine[None, None, None]] | None
    ) = None,
) -> T:
    """Execute fn with retry/backoff governed by the category's strategy.

    Args:
        fn: Async callable to retry.
        category: Failure category determining the retry strategy.
        on_retry: Optional callback invoked before each retry with (category, attempt, exception).

    Returns:
        The result of fn on success.

    Raises:
        The last exception if all retry attempts are exhausted.
    """
    strategy = RETRY_MATRIX[category]

    if not strategy.retryable:
        return await fn()

    last_exc: Exception | None = None
    for attempt in range(strategy.max_attempts):
        try:
            return await fn()
        except Exception as exc:
            last_exc = exc
            if attempt < strategy.max_attempts - 1:
                delay = backoff_delay(strategy, attempt)
                if on_retry:
                    await on_retry(category, attempt, exc)
                await asyncio.sleep(delay)

    assert last_exc is not None
    raise last_exc
