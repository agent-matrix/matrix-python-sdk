# matrix_sdk/bulk/backoff.py
"""Generic retry/backoff decorator for async functions."""
from __future__ import annotations

import asyncio
import functools
import random
from typing import Any, Callable


def with_backoff(
    max_retries: int = 5, base_delay: float = 1.0, jitter: float = 0.1
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Retry an async function with exponential backoff + jitter."""

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
                        raise
                    delay = base_delay * (2 ** (attempt - 1)) + random.uniform(
                        0, jitter
                    )
                    await asyncio.sleep(delay)

        return wrapper

    return decorator
