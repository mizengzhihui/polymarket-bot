"""
Polymarket Leaderboard & Trader Data
Fetches top traders, their positions, and recent trades.
"""
import json, sys, os, requests
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from common.rate_limit import rate_limited
from common.api_monitor import monitored
from config import DATA_API, GAMMA_API

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
    try:
        os.makedirs(os.path.dirname(_VALUE_CACHE_FILE), exist_ok=True)
        tmp = _VALUE_CACHE_FILE + ".tmp"
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(cache, f)
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


@monitored("poly_leaderboard")
def get_leaderboard(category="OVERALL", time_period="MONTH", order_by="PNL", limit=25):
    """Fetch top traders from Polymarket leaderboard."""
    try:
        resp = requests.get(
            f"{DATA_API}/v1/leaderboard",
            params={
                "category": category,
                "timePeriod": time_period,
                "orderBy": order_by,
                "limit": limit,
            },
            timeout=15,
        )
        resp.raise_for_status()
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
        resp = requests.get(
            f"{GAMMA_API}/public-profile", params={"address": wallet}, timeout=10
        )
        resp.raise_for_status()
        return resp.json()
    except Exception:
        return {}


def get_trader_value(wallet):
    """Get estimated portfolio value for a trader.
    Priority: API value (>$1000) > disk cache (>$1000) > positions initial_value > leaderboard PnL/3 > 0.
    Values below $1000 from API are considered stale (positions resolved) and fall through."""
    value = 0.0

    # 1) Try value API
    try:
        resp = requests.get(
            f"{DATA_API}/value", params={"user": wallet}, timeout=10
        )
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, list):
            data = data[0] if data else {}
        value = float(data.get("value", 0))
        tag = "CACHE_HIT" if (value == 0 and wallet in _trader_value_cache) else "OK"
        print(f"  [trader_value] {wallet[:10]}... = ${value:,.0f} [{tag}] raw={data}")
        if value >= 1000:  # Only trust values >= $1000 (positions still active)
            _trader_value_cache[wallet] = value
            _save_value_cache(_trader_value_cache)
            return value
    except Exception:
        pass

    # 2) Try disk cache (from previous API or positions fallback)
    cached = _trader_value_cache.get(wallet, 0)
    if cached >= 1000:
        print(f"  [trader_value] {wallet[:10]}... = ${cached:,.0f} [DISK_CACHE]")
        return cached

    # 3) Try positions total initial_value as floor estimate
    try:
        resp = requests.get(
            f"{DATA_API}/positions",
            params={"user": wallet, "limit": 500},
            timeout=15,
        )
        resp.raise_for_status()
        total_initial = sum(float(p.get("initialValue", 0) or 0) for p in resp.json())
        if total_initial >= 1000:
            print(f"  [trader_value] {wallet[:10]}... = ${total_initial:,.0f} [POSITIONS_FALLBACK]")
            _trader_value_cache[wallet] = total_initial
            _save_value_cache(_trader_value_cache)
            return total_initial
    except Exception:
        pass

    # 4) Try leaderboard monthly PnL / 3 as rough portfolio estimate
    try:
        resp = requests.get(
            f"{DATA_API}/v1/leaderboard",
            params={"category": "OVERALL", "timePeriod": "MONTH", "orderBy": "PNL", "limit": 50},
            timeout=15,
        )
        resp.raise_for_status()
        for entry in resp.json():
            if entry.get("proxyWallet", "").lower() == wallet.lower():
                pnl = float(entry.get("pnl", 0) or 0)
                if pnl > 0:
                    estimate = max(pnl / 3, cached, value)
                    print(f"  [trader_value] {wallet[:10]}... = ${estimate:,.0f} [LB_PNL/${pnl:,.0f}]")
                    if estimate >= 1000:
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
        resp = requests.get(
            f"{DATA_API}/positions",
            params={"user": wallet, "sizeThreshold": size_threshold, "limit": 500},
            timeout=15,
        )
        resp.raise_for_status()
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
            resp = requests.get(f"{DATA_API}/trades", params=params, timeout=15)
            resp.raise_for_status()
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
        resp = requests.get(
            f"{GAMMA_API}/markets", params={"condition_id": condition_id}, timeout=10
        )
        resp.raise_for_status()
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


# ============================================================
# Auto-discovery (v1.3)
# ============================================================
import time as _time
from config import (
    safe_get, LEADERBOARD_TOP_N, LEADERBOARD_CATEGORIES,
    NEWBIE_MIN_TRADES, NEWBIE_MIN_PROFIT_USD, NEWBIE_LOOKBACK_DAYS,
)


def get_leaderboard_v2(category="monthly", limit=20):
    """Fetch top traders from Polymarket leaderboard (v1.3 format).
    Uses /v1/leaderboard endpoint with OVERALL category and PNL ordering.
    """
    period_map = {"daily": "24H", "weekly": "7D", "monthly": "MONTH",
                  "yearly": "YEAR", "all": "ALL"}
    period = period_map.get(category, "MONTH")
    params = {"category": "OVERALL", "timePeriod": period,
              "orderBy": "PNL", "limit": min(limit, 100)}
    try:
        resp = safe_get(f"{DATA_API}/v1/leaderboard", params=params, timeout=10)
        data = resp.json() if resp and resp.status_code == 200 else []
        traders = []
        for entry in data:
            addr = (entry.get("proxyWallet") or entry.get("address")
                    or entry.get("trader") or "").strip().lower()
            if not addr or addr == "0x0000000000000000000000000000000000000000":
                continue
            traders.append({
                "address": addr,
                "rank": int(entry.get("rank", 0)),
                "volume": float(entry.get("vol", 0)),
                "profit": float(entry.get("pnl", 0)),
                "source": f"leaderboard_{category}",
            })
        return traders
    except Exception:
        return []


def get_all_leaderboards(top_n=20):
    """Aggregate traders from all leaderboard categories."""
    all_traders = {}
    for cat in LEADERBOARD_CATEGORIES:
        traders = get_leaderboard_v2(cat, limit=top_n)
        for t in traders:
            addr = t["address"]
            if addr not in all_traders:
                all_traders[addr] = t
                all_traders[addr]["categories"] = [cat]
            else:
                all_traders[addr]["categories"].append(cat)
                if t.get("rank", 999) < all_traders[addr].get("rank", 999):
                    all_traders[addr]["rank"] = t["rank"]
                    all_traders[addr]["profit"] = t.get("profit", all_traders[addr]["profit"])
                    all_traders[addr]["volume"] = t.get("volume", all_traders[addr]["volume"])
    return list(all_traders.values())


def scan_newbies():
    """Scan recent trades for promising new traders not on leaderboards."""
    candidates = []
    try:
        resp = safe_get(f"{DATA_API}/trades", params={"limit": 200}, timeout=10)
        if not resp or resp.status_code != 200:
            return []
        trades = resp.json()
        wallet_stats = {}
        now = _time.time()
        cutoff = now - NEWBIE_LOOKBACK_DAYS * 86400
        for t in trades:
            addr = (t.get("proxyWallet") or t.get("maker") or "").strip().lower()
            ts = int(t.get("timestamp", 0))
            if not addr or ts < cutoff:
                continue
            size = float(t.get("size", 0))
            price = float(t.get("price", 0))
            side = t.get("side", "BUY")
            if addr not in wallet_stats:
                wallet_stats[addr] = {"trades": 0, "wins": 0,
                                      "total_pnl": 0.0, "buy_volume": 0.0,
                                      "sell_volume": 0.0}
            s = wallet_stats[addr]
            s["trades"] += 1
            cost = size * price
            if side == "BUY":
                s["buy_volume"] += cost
            else:
                s["sell_volume"] += cost
        for addr, stats in wallet_stats.items():
            if stats["trades"] < NEWBIE_MIN_TRADES:
                continue
            if stats["buy_volume"] > 0:
                net_profit = stats["sell_volume"] - stats["buy_volume"]
                roi = net_profit / stats["buy_volume"]
            else:
                continue
            if net_profit >= NEWBIE_MIN_PROFIT_USD and roi >= 0:
                candidates.append({
                    "address": addr,
                    "trades_7d": stats["trades"],
                    "net_profit": round(net_profit, 2),
                    "roi": round(roi, 4),
                    "source": "newbie_scan",
                })
        return candidates
    except Exception:
        return []
