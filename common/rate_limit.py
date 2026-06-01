"""
API Rate Limiter — token bucket / per-second throttling.
Used by trader.py and leaderboard.py to avoid hitting Polymarket rate limits.
"""
import time
import functools


class TokenBucket:
    """Simple token bucket rate limiter."""

    def __init__(self, max_calls_per_second=5):
        self.rate = max_calls_per_second
        self.tokens = float(max_calls_per_second)
        self.last_refill = time.time()

    def _refill(self):
        now = time.time()
        elapsed = now - self.last_refill
        self.tokens = min(self.rate, self.tokens + elapsed * self.rate)
        self.last_refill = now

    def consume(self, tokens=1):
        self._refill()
        if self.tokens >= tokens:
            self.tokens -= tokens
            return True
        return False

    def wait(self, tokens=1):
        while not self.consume(tokens):
            time.sleep(0.05)


# Global rate limiter instances (one per API endpoint group)
_global_limiter = TokenBucket(max_calls_per_second=5)


def rate_limited(max_calls_per_second=5):
    """Decorator: throttle function calls to max_calls_per_second."""
    limiter = TokenBucket(max_calls_per_second)

    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            limiter.wait()
            return func(*args, **kwargs)
        return wrapper
    return decorator


def wait_if_needed():
    """Block until the global limiter allows the next call."""
    _global_limiter.wait()
