"""Shared API call monitor — tracks count, errors, latency per endpoint.

Usage:
    from common.api_monitor import monitored, get_stats, save_stats

    @monitored("okx_get_balance")
    def get_balance():
        ...

    # In main loop, periodically:
    stats = get_stats()
    save_stats("results/api_stats.json")
"""

import time
import json
import os
import threading
from functools import wraps

_lock = threading.Lock()
_stats: dict[str, dict] = {}  # name -> {calls, errors, total_latency_ms, last_error}


def monitored(name: str):
    """Decorator: track call count, error rate, and latency for a function."""
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            t0 = time.time()
            try:
                result = func(*args, **kwargs)
                elapsed_ms = (time.time() - t0) * 1000
                with _lock:
                    s = _stats.setdefault(name, {"calls": 0, "errors": 0, "total_latency_ms": 0.0, "last_error": ""})
                    s["calls"] += 1
                    s["total_latency_ms"] += elapsed_ms
                return result
            except Exception as e:
                elapsed_ms = (time.time() - t0) * 1000
                with _lock:
                    s = _stats.setdefault(name, {"calls": 0, "errors": 0, "total_latency_ms": 0.0, "last_error": ""})
                    s["calls"] += 1
                    s["errors"] += 1
                    s["total_latency_ms"] += elapsed_ms
                    s["last_error"] = str(e)[:200]
                raise
        return wrapper
    return decorator


def get_stats() -> dict:
    """Return a snapshot of current API stats."""
    with _lock:
        result = {}
        for name, s in _stats.items():
            calls = s["calls"]
            errors = s["errors"]
            total_ms = s["total_latency_ms"]
            result[name] = {
                "calls": calls,
                "errors": errors,
                "error_rate": round(errors / calls, 4) if calls > 0 else 0,
                "avg_latency_ms": round(total_ms / calls, 1) if calls > 0 else 0,
                "total_latency_ms": round(total_ms, 1),
                "last_error": s["last_error"][:150] if s["last_error"] else "",
            }
        return result


def save_stats(path: str):
    """Persist API stats to a JSON file (atomic write)."""
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(get_stats(), f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception:
        pass


def reset_stats():
    """Reset all accumulated stats (call at midnight rollover)."""
    with _lock:
        _stats.clear()
