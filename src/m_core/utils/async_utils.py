"""Async utilities."""
import asyncio
from typing import Callable, Any, List, TypeVar, Awaitable
from functools import wraps

T = TypeVar("T")


async def run_in_thread(func: Callable[..., T], *args, **kwargs) -> T:
    """Run a synchronous function in a thread pool."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, lambda: func(*args, **kwargs))


async def gather_with_concurrency(
    limit: int,
    *coros: Awaitable[T],
    return_exceptions: bool = False,
) -> List[T]:
    """Gather coroutines with concurrency limit."""
    semaphore = asyncio.Semaphore(limit)
    
    async def limited(coro: Awaitable[T]) -> T:
        async with semaphore:
            return await coro
    
    return await asyncio.gather(
        *(limited(coro) for coro in coros),
        return_exceptions=return_exceptions,
    )


def async_cached(func: Callable[..., Awaitable[T]]) -> Callable[..., Awaitable[T]]:
    """Cache async function results."""
    cache = {}
    
    @wraps(func)
    async def wrapper(*args, **kwargs):
        key = (args, tuple(sorted(kwargs.items())))
        if key not in cache:
            cache[key] = await func(*args, **kwargs)
        return cache[key]
    
    wrapper.cache = cache
    wrapper.cache_clear = lambda: cache.clear()
    return wrapper