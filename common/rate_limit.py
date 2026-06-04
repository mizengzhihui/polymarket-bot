"""Shared rate limiter for Polymarket API calls."""
import time
import threading
from functools import wraps


def rate_limited(max_calls_per_second: float = 5.0):
    """Decorator: limit function to max_calls_per_second across all threads.

    Polymarket's public API is generous (~10 req/s), but we stay safe at 5.
    """
    lock = threading.Lock()
    last_call = [0.0]  # mutable for closure capture
    min_interval = 1.0 / max_calls_per_second

    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            with lock:
                elapsed = time.time() - last_call[0]
                wait = max(0, min_interval - elapsed)
            if wait > 0:
                time.sleep(wait)
            result = func(*args, **kwargs)
            with lock:
                last_call[0] = time.time()
            return result
        return wrapper
    return decorator
