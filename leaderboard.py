"""
Polymarket Leaderboard & Trader Data
Fetches top traders, their positions, and recent trades.
"""
import json, sys, os, requests, time as time_module
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from common.rate_limit import rate_limited
from common.api_monitor import monitored
from config import DATA_API, GAMMA_API, safe_get

_VALUE_CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results", "trader_value_cache.json")


def _load_value_cache():
    try:
        if os.path.exists(_VALUE_CACHE_FILE):
            with open(_VALUE_CACHE_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
    except Exception:
        pass
    return {}


def _save_value_cache(cache):
    """Persist value cache to disk with timestamps for freshness tracking."""
    try:
        now = int(time_module.time())
        ts_cache = {}
        for k, v in cache.items():
            if isinstance(v, dict):
                ts_cache[k] = v
            else:
                ts_cache[k] = {"value": v, "ts": now}
        os.makedirs(os.path.dirname(_VALUE_CACHE_FILE), exist_ok=True)
        tmp = _VALUE_CACHE_FILE + ".tmp"
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(ts_cache, f)
        os.replace(tmp, _VALUE_CACHE_FILE)
    except Exception:
        pass


def _norm_usdc(v):
    """Normalize raw USDC units (6 decimals) to USD."""
    try:
        f = float(v or 0)
        return f / 1e6 if abs(f) > 100000 else f
    except (TypeError, ValueError):
        return 0.0


_trader_value_cache = _load_value_cache()  # wallet -> last non-zero value (survives restarts)
VALUE_CACHE_MAX_AGE = 3600  # 1 hour: cache entries older than this are stale


@monitored("poly_leaderboard")
def get_leaderboard(category="OVERALL", time_period="MONTH", order_by="PNL", limit=25):
    """Fetch top traders from Polymarket leaderboard."""
    try:
        resp = safe_get(
            f"{DATA_API}/v1/leaderboard",
            params={
                "category": category,
                "timePeriod": time_period,
                "orderBy": order_by,
                "limit": limit,
            },
            timeout=15,
        )
        data = resp.json()
        traders = []
        for entry in data:
            traders.append({
                "rank": entry.get("rank"),
                "wallet": entry.get("proxyWallet", ""),
                "username": entry.get("userName", ""),
                "pnl": float(entry.get("pnl", 0)),
                "volume": float(entry.get("vol", 0)),
                "x_username": entry.get("xUsername", ""),
                "verified": entry.get("verifiedBadge", False),
            })
        return traders
    except Exception:
        return []


def get_trader_profile(wallet):
    """Fetch public profile for a wallet."""
    try:
        resp = safe_get(
            f"{GAMMA_API}/public-profile", params={"address": wallet}, timeout=10
        )
        return resp.json()
    except Exception:
        return {}


def get_trader_value(wallet):
    """Get estimated portfolio value for a trader.
    Priority: API value (> 0) > disk cache (fresh) > positions initial_value > leaderboard PnL/3 > 0.
    API values are ALWAYS trusted and update cache. Old cache (>1h stale) is discarded."""
    try:
        resp = safe_get(
            f"{DATA_API}/value", params={"user": wallet}, timeout=10
        )
        data = resp.json()
        if isinstance(data, list):
            data = data[0] if data else {}
        value = float(data.get("value", 0))
        tag = "CACHE_HIT" if (value == 0 and wallet in _trader_value_cache) else "OK"
        print(f"  [trader_value] {wallet[:10]}... = ${value:,.0f} [{tag}] raw={data}")
        if value > 0:  # Always trust API values (fix: cache deadlock)
            _trader_value_cache[wallet] = value
            _save_value_cache(_trader_value_cache)
            return value
    except Exception:
        pass

    # 2) Try disk cache (from previous API or positions fallback)
    cached = _trader_value_cache.get(wallet, 0)
    if isinstance(cached, dict):
        # New format: {"value": ..., "ts": ...}
        cache_ts = cached.get("ts", 0)
        cache_val = cached.get("value", 0)
    else:
        # Old format: raw number
        cache_ts = 0
        cache_val = cached

    if cache_val > 0:
        cache_age = time_module.time() - cache_ts if cache_ts > 0 else 999999
        is_stale = cache_ts > 0 and cache_age > VALUE_CACHE_MAX_AGE
        if is_stale:
            print(f"  [trader_value] {wallet[:10]}... = ${cache_val:,.0f} [STALE {cache_age/60:.0f}min - skipping, using API]")
        else:
            print(f"  [trader_value] {wallet[:10]}... = ${cache_val:,.0f} [DISK_CACHE]")
            return cache_val

    # 3) Try positions total initial_value as floor estimate
    try:
        resp = safe_get(
            f"{DATA_API}/positions",
            params={"user": wallet, "limit": 500},
            timeout=15,
        )
        total_initial = sum(float(p.get("initialValue", 0) or 0) for p in resp.json())
        if total_initial > 0:
            print(f"  [trader_value] {wallet[:10]}... = ${total_initial:,.0f} [POSITIONS_FALLBACK]")
            _trader_value_cache[wallet] = total_initial
            _save_value_cache(_trader_value_cache)
            return total_initial
    except Exception:
        pass

    # 4) Try leaderboard monthly PnL / 3 as rough portfolio estimate
    try:
        resp = safe_get(
            f"{DATA_API}/v1/leaderboard",
            params={"category": "OVERALL", "timePeriod": "MONTH", "orderBy": "PNL", "limit": 50},
            timeout=15,
        )
        for entry in resp.json():
            if entry.get("proxyWallet", "").lower() == wallet.lower():
                pnl = float(entry.get("pnl", 0) or 0)
                if pnl > 0:
                    estimate = max(pnl / 3, cached, value)
                    print(f"  [trader_value] {wallet[:10]}... = ${estimate:,.0f} [LB_PNL/${pnl:,.0f}]")
                    if estimate > 0:
                        _trader_value_cache[wallet] = estimate
                        _save_value_cache(_trader_value_cache)
                    return estimate
                break
    except Exception:
        pass

    # 5) Last resort: return whatever we have (even if tiny)
    if value > 0 or cached > 0:
        return max(value, cached)
    print(f"  [trader_value] {wallet[:10]}... = $0 [ALL_FAILED]")
    return 0.0


@rate_limited(max_calls_per_second=5)
@monitored("poly_trader_positions")
def get_trader_positions(wallet, size_threshold=10):
    """Fetch open positions for a wallet."""
    try:
        resp = safe_get(
            f"{DATA_API}/positions",
            params={"user": wallet, "sizeThreshold": size_threshold, "limit": 500},
            timeout=15,
        )
        positions = []
        for p in resp.json():
            positions.append({
                "condition_id": p.get("conditionId"),
                "asset": p.get("asset"),
                "title": p.get("title", ""),
                "outcome": p.get("outcome", ""),
                "size": float(p.get("size", 0)),
                "avg_price": float(p.get("avgPrice", 0)),
                "current_price": float(p.get("curPrice", 0)),
                "initial_value": float(p.get("initialValue", 0)),
                "current_value": float(p.get("currentValue", 0)),
                "cash_pnl": float(p.get("cashPnl", 0)),
                "percent_pnl": float(p.get("percentPnl", 0)),
                "realized_pnl": float(p.get("realizedPnl", 0)),
            })
        return positions
    except Exception:
        return []


@rate_limited(max_calls_per_second=5)
@monitored("poly_trader_trades")
def get_trader_trades(wallet, limit=1000, since_timestamp=None):
    """Fetch recent trades for a wallet. Uses server-side pagination when available."""
    params = {"user": wallet, "limit": limit, "takerOnly": True}
    # If we have a timestamp, try server-side filtering first
    if since_timestamp:
        # Polymarket API supports startTs for server-side filtering
        params["startTs"] = since_timestamp
    try:
        all_trades = []
        seen_tx = set()
        max_pages = 10  # hard cap to prevent infinite loops
        for _ in range(max_pages):
            resp = safe_get(f"{DATA_API}/trades", params=params, timeout=15)
            batch = resp.json()
            if not batch:
                break
            new_in_batch = 0
            for t in batch:
                tx = t.get("transactionHash", "")
                if tx and tx in seen_tx:
                    continue
                if tx:
                    seen_tx.add(tx)
                ts = int(t.get("timestamp", 0))
                if since_timestamp and ts <= since_timestamp:
                    continue
                all_trades.append({
                    "tx_hash": tx,
                    "timestamp": ts,
                    "side": t.get("side", ""),
                    "asset": t.get("asset"),
                    "condition_id": t.get("conditionId"),
                    "title": t.get("title", ""),
                    "outcome": t.get("outcome", ""),
                    "size": float(t.get("size", 0)),
                    "price": float(t.get("price", 0)),
                    "trader": t.get("proxyWallet", wallet),
                })
                new_in_batch += 1
            if new_in_batch == 0:   # pagination exhausted (no new unique trades)
                break
            if len(batch) < limit:
                break
            oldest_ts = min(int(t.get("timestamp", 0)) for t in batch if t.get("timestamp"))
            params["beforeTimestamp"] = oldest_ts
        return all_trades
    except Exception:
        return []


@rate_limited(max_calls_per_second=5)
def get_market_info(condition_id):
    """Get market details. Returns None if unresolvable (CLOB will reject anyway)."""
    try:
        resp = safe_get(
            f"{GAMMA_API}/markets", params={"condition_id": condition_id}, timeout=10
        )
        markets = resp.json()
        for m in markets:
            if m.get("conditionId") == condition_id:
                return {
                    "condition_id": m.get("conditionId"),
                    "title": m.get("title", ""),
                    "clob_token_ids": m.get("clobTokenIds", []),
                    "outcomes": m.get("outcomes", []),
                    "end_date": m.get("endDate", ""),
                    "resolved": m.get("resolved", False),
                }
    except Exception:
        pass
    return None
