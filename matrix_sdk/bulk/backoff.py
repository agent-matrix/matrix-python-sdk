# matrix_sdk/bulk/backoff.py
"""
Generic retry/backoff decorator for async functions.
"""
import asyncio
import functools
import random
from typing import Any, Callable


def with_backoff(
    max_retries: int = 5, base_delay: float = 1.0, jitter: float = 0.1
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """
    Decorator to retry an async function with exponential backoff and jitter.

    Args:
        max_retries: Number of retry attempts.
        base_delay: Initial delay in seconds before retry.
        jitter: Max random jitter added to delay.

    Returns:
        Decorated async function that retries on exception.
    """

    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            attempt = 0
            while True:
                try:
                    return await fn(*args, **kwargs)
                except Exception:
                    attempt += 1
                    if attempt > max_retries:
                        # Exhausted retries: propagate exception
                        raise
                    # Exponential backoff delay
                    delay = base_delay * (2 ** (attempt - 1))
                    # Add random jitter
                    delay += random.uniform(0, jitter)
                    await asyncio.sleep(delay)

        return wrapper

    return decorator
