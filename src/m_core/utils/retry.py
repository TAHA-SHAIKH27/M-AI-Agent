"""Retry policies and decorators."""
import asyncio
import random
from typing import Callable, Any, Tuple, Type, Optional, TypeVar
from dataclasses import dataclass
from functools import wraps

T = TypeVar("T")


@dataclass
class RetryPolicy:
    """Configuration for retry behavior."""
    max_attempts: int = 3
    base_delay: float = 1.0
    max_delay: float = 60.0
    exponential_base: float = 2.0
    jitter: bool = True
    retry_exceptions: Tuple[Type[Exception], ...] = (Exception,)
    should_retry: Optional[Callable[[Exception], bool]] = None
    
    def get_delay(self, attempt: int) -> float:
        """Calculate delay for attempt number."""
        delay = min(
            self.base_delay * (self.exponential_base ** attempt),
            self.max_delay
        )
        if self.jitter:
            delay *= (0.5 + random.random())  # 0.5 to 1.5 multiplier
        return delay


def retry(policy: Optional[RetryPolicy] = None) -> Callable:
    """Decorator for retrying async functions."""
    if policy is None:
        policy = RetryPolicy()
    
    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        @wraps(func)
        async def wrapper(*args, **kwargs) -> Any:
            last_exception = None
            
            for attempt in range(policy.max_attempts):
                try:
                    return await func(*args, **kwargs)
                except policy.retry_exceptions as e:
                    last_exception = e
                    
                    # Check custom retry condition
                    if policy.should_retry and not policy.should_retry(e):
                        raise
                    
                    if attempt < policy.max_attempts - 1:
                        delay = policy.get_delay(attempt)
                        import structlog
                        logger = structlog.get_logger(func.__module__)
                        logger.warning(
                            f"Retry attempt {attempt + 1}/{policy.max_attempts} for {func.__name__}",
                            error=str(e),
                            delay=delay,
                        )
                        await asyncio.sleep(delay)
                    else:
                        break
            
            # All retries exhausted
            raise last_exception
        
        return wrapper
    return decorator


# Common retry policies
DEFAULT_RETRY = RetryPolicy(max_attempts=3, base_delay=1.0)
NETWORK_RETRY = RetryPolicy(
    max_attempts=5,
    base_delay=2.0,
    max_delay=30.0,
    retry_exceptions=(ConnectionError, TimeoutError, IOError),
)
API_RETRY = RetryPolicy(
    max_attempts=3,
    base_delay=1.0,
    retry_exceptions=(Exception,),
    should_retry=lambda e: not isinstance(e, (ValueError, TypeError)),
)