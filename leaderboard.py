
"""Leaderboard and Trader Discovery (v1.3)."""
import json, os, time
from config import DATA_API, GAMMA_API, safe_get, LEADERBOARD_TOP_N, LEADERBOARD_CATEGORIES, NEWBIE_MIN_TRADES, NEWBIE_MIN_WINRATE, NEWBIE_MIN_PROFIT_USD, NEWBIE_LOOKBACK_DAYS

BASE = os.path.dirname(os.path.abspath(__file__))

def get_leaderboard(category="monthly", limit=20):
    """Fetch top traders from Polymarket leaderboard.
    Uses /v1/leaderboard endpoint with OVERALL category and PNL ordering.
    """
    period_map = {"daily": "24H", "weekly": "7D", "monthly": "MONTH", "yearly": "YEAR", "all": "ALL"}
    period = period_map.get(category, "MONTH")
    params = {"category": "OVERALL", "timePeriod": period, "orderBy": "PNL", "limit": min(limit, 100)}
    try:
        resp = safe_get(f"{DATA_API}/v1/leaderboard", params=params, timeout=10)
        data = resp.json() if resp and resp.status_code == 200 else []
        traders = []
        for entry in data:
            addr = (entry.get("address") or entry.get("trader") or "").strip().lower()
            if not addr or addr == "0x0000000000000000000000000000000000000000":
                continue
            traders.append({
                "address": addr, "rank": int(entry.get("rank", 0)),
                "volume": float(entry.get("vol", 0)),
                "profit": float(entry.get("pnl", 0)),
                "source": f"leaderboard_{category}",
            })
        return traders
    except Exception:
        return []

def get_all_leaderboards(top_n=20):
    all_traders = {}
    for cat in LEADERBOARD_CATEGORIES:
        traders = get_leaderboard(cat, limit=top_n)
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
                    all_traders[addr]["win_rate"] = t.get("win_rate", all_traders[addr]["win_rate"])
                    all_traders[addr]["roi"] = t.get("roi", all_traders[addr]["roi"])
    return list(all_traders.values())

def scan_newbies():
    candidates = []
    try:
        resp = safe_get(f"{DATA_API}/trades", params={"limit": 200}, timeout=10)
        if not resp or resp.status_code != 200:
            return []
        trades = resp.json()
        wallet_stats = {}
        now = time.time()
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
                wallet_stats[addr] = {"trades": 0, "wins": 0, "total_pnl": 0.0, "buy_volume": 0.0, "sell_volume": 0.0}
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
                    "address": addr, "trades_7d": stats["trades"],
                    "net_profit": round(net_profit, 2), "roi": round(roi, 4),
                    "source": "newbie_scan",
                })
        return candidates
    except Exception:
        return []

def get_trader_profile(wallet):
    try:
        resp = safe_get(f"{GAMMA_API}/profile/{wallet}", timeout=10)
        if resp and resp.status_code == 200:
            return resp.json()
    except Exception:
        pass
    return {}

def get_trader_value(wallet):
    try:
        resp = safe_get(f"{DATA_API}/value", params={"user": wallet}, timeout=10)
        if resp and resp.status_code == 200:
            return float(resp.json().get("value", 0))
    except Exception:
        pass
    return 0

def get_trader_trades(wallet, limit=50):
    try:
        resp = safe_get(f"{DATA_API}/trades", params={"user": wallet, "limit": limit}, timeout=10)
        if resp and resp.status_code == 200:
            return resp.json()
    except Exception:
        pass
    return []

def get_trader_positions(wallet, size_threshold=1):
    try:
        resp = safe_get(f"{DATA_API}/positions", params={"user": wallet, "sizeThreshold": size_threshold}, timeout=10)
        if resp and resp.status_code == 200:
            return resp.json()
    except Exception:
        pass
    return []

def get_market_info(condition_id):
    try:
        resp = safe_get(f"{GAMMA_API}/markets", params={"condition_id": condition_id}, timeout=10)
        if resp and resp.status_code == 200:
            data = resp.json()
            if data:
                return data[0]
    except Exception:
        pass
    return {}
