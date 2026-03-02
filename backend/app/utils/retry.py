"""
Retry utility with exponential backoff for external API calls.

Usage:
    from app.utils.retry import with_retries

    # Async function
    result = await with_retries(
        lambda: client.get(url, headers=headers),
        max_retries=3,
        base_delay=1.0,
        operation="PostHog fetch sessions",
    )

    # Works with any async callable
    response = await with_retries(
        lambda: openai_client.chat.completions.create(...),
        max_retries=2,
        base_delay=2.0,
        retryable_exceptions=(Exception,),
        operation="OpenAI analysis",
    )
"""

from __future__ import annotations

import asyncio
import random
from typing import Any, Awaitable, Callable, Type

from app.utils.logger import logger


# Exceptions that are safe to retry (transient failures)
_DEFAULT_RETRYABLE = (
    ConnectionError,
    TimeoutError,
    OSError,
)

# HTTP status codes worth retrying
_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}


async def with_retries(
    fn: Callable[[], Awaitable[Any]],
    *,
    max_retries: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
    retryable_exceptions: tuple[Type[Exception], ...] | None = None,
    operation: str = "API call",
) -> Any:
    """
    Execute an async function with exponential backoff retry.

    Args:
        fn: Async callable (no args) to execute. Use lambda for partial application.
        max_retries: Maximum number of retry attempts (0 = no retries).
        base_delay: Initial delay in seconds (doubled each retry + jitter).
        max_delay: Maximum delay between retries.
        retryable_exceptions: Exception types to retry on.
            Defaults to ConnectionError, TimeoutError, OSError.
        operation: Human-readable name for logging.

    Returns:
        The result of fn().

    Raises:
        The last exception if all retries are exhausted.
    """
    if retryable_exceptions is None:
        retryable_exceptions = _DEFAULT_RETRYABLE

    last_exc: Exception | None = None

    for attempt in range(max_retries + 1):
        try:
            result = await fn()

            # Check for httpx Response objects with retryable status codes
            if hasattr(result, "status_code") and result.status_code in _RETRYABLE_STATUS_CODES:
                if attempt < max_retries:
                    delay = _calc_delay(attempt, base_delay, max_delay)
                    logger.warning(
                        f"{operation}: HTTP {result.status_code}, retrying in {delay:.1f}s "
                        f"(attempt {attempt + 1}/{max_retries + 1})"
                    )
                    await asyncio.sleep(delay)
                    continue
                else:
                    # Last attempt — let the caller handle the bad status
                    return result

            return result

        except retryable_exceptions as exc:
            last_exc = exc
            if attempt < max_retries:
                delay = _calc_delay(attempt, base_delay, max_delay)
                logger.warning(
                    f"{operation}: {type(exc).__name__}: {exc}, retrying in {delay:.1f}s "
                    f"(attempt {attempt + 1}/{max_retries + 1})"
                )
                await asyncio.sleep(delay)
            else:
                logger.error(
                    f"{operation}: {type(exc).__name__}: {exc}, "
                    f"all {max_retries + 1} attempts exhausted"
                )
                raise

    # Should not reach here, but just in case
    if last_exc:
        raise last_exc
    raise RuntimeError(f"{operation}: unexpected retry loop exit")


def _calc_delay(attempt: int, base_delay: float, max_delay: float) -> float:
    """Calculate delay with exponential backoff + jitter."""
    delay = base_delay * (2 ** attempt)
    # Add ±25% jitter to prevent thundering herd
    jitter = delay * 0.25 * (2 * random.random() - 1)
    return min(delay + jitter, max_delay)
