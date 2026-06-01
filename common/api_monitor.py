"""
API Monitor — tracks request counts, errors, and latency for Polymarket API calls.
Used by leaderboard.py and bot.py for observability.
"""
import time
import functools
import json
import os
import logging

logger = logging.getLogger(__name__)

# In-memory stats
_stats = {
    "total_calls": 0,
    "errors": 0,
    "by_endpoint": {},
    "started": time.time(),
    "last_reset": time.time(),
}


def monitored(endpoint_name=None):
    """Decorator: record API call stats for an endpoint."""
    def decorator(func):
        name = endpoint_name or func.__name__

        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            start = time.time()
            _stats["total_calls"] += 1
            try:
                result = func(*args, **kwargs)
                latency = time.time() - start
                ep_stats = _stats["by_endpoint"].setdefault(name, {"calls": 0, "errors": 0, "total_latency": 0.0})
                ep_stats["calls"] += 1
                ep_stats["total_latency"] += latency
                return result
            except Exception as e:
                _stats["errors"] += 1
                ep_stats = _stats["by_endpoint"].setdefault(name, {"calls": 0, "errors": 0, "total_latency": 0.0})
                ep_stats["errors"] += 1
                raise
        return wrapper
    return decorator


def get_stats():
    """Return a copy of current API stats."""
    return dict(_stats)


def save_stats(path):
    """Persist API stats to a JSON file."""
    try:
        s = dict(_stats)
        s["uptime"] = time.time() - s["started"]
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(s, f, indent=2)
        os.replace(tmp, path)
    except Exception as e:
        logger.error(f"Failed to save API stats: {e}")


def reset_stats():
    """Reset all API stats (called daily at midnight rollover)."""
    _stats["total_calls"] = 0
    _stats["errors"] = 0
    _stats["by_endpoint"] = {}
    _stats["last_reset"] = time.time()
