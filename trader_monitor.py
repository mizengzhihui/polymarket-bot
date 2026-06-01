"""
Trader Behavior Monitor — detects style drift and account anomalies.

Tracks per-wallet daily behavior snapshots. Compares recent 7-day window
against 30-day baseline. Alerts on significant deviations in:
  - Trade frequency
  - Average position size
  - Market category mix
  - Buy/sell direction ratio
"""
import json
import os
import time
from collections import defaultdict
from datetime import datetime, timezone

MONITOR_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "results", "trader_monitor.json")

CATEGORY_KEYWORDS = {
    "政治": ["election", "president", "trump", "biden", "senate", "congress",
             "vote", "democrat", "republican", "governor", "campaign", "primary",
             "cabinet", "parliament", "ballot", "referendum"],
    "加密": ["btc", "eth", "bitcoin", "ethereum", "crypto", "sol", "solana",
             "token", "defi", "nft", "blockchain", "layer", "l2"],
    "体育": ["championship", "super bowl", "nba", "nfl", "playoff",
             "final", "cup", "league", "season", "title"],
    "经济": ["fed", "rate", "inflation", "gdp", "cpi", "recession",
             "market", "stock", "sp500", "tariff", "treasury", "yield"],
}


def _classify(title: str) -> str:
    t = (title or "").lower()
    for cat, keywords in CATEGORY_KEYWORDS.items():
        for kw in keywords:
            if kw in t:
                return cat
    return "其他"


class TraderMonitor:
    def __init__(self):
        self._daily_stats: dict[str, list[dict]] = {}   # wallet -> [snap, ...]
        self._baseline: dict[str, dict] = {}             # wallet -> baseline stats
        self._restore()

    # ------------------------------------------------------------------
    # Data input
    # ------------------------------------------------------------------
    def update_daily(self, wallet: str, trades: list[dict]):
        """Record daily snapshot from a batch of recent trades."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        cutoff = int(time.time()) - 86400

        day_trades = [t for t in trades
                      if int(t.get("timestamp", 0)) >= cutoff]
        if not day_trades:
            return

        sizes = [float(t.get("size", 0)) * float(t.get("price", 0))
                 for t in day_trades]
        avg_size = sum(sizes) / len(sizes)

        cats = defaultdict(int)
        for t in day_trades:
            cats[_classify(t.get("title", ""))] += 1
        total = sum(cats.values()) or 1
        cat_pct = {k: round(v / total * 100, 1) for k, v in cats.items()}

        buys = sum(1 for t in day_trades if t.get("side") == "BUY")
        sells = sum(1 for t in day_trades if t.get("side") == "SELL")

        snap = {
            "date": today,
            "trades": len(day_trades),
            "avg_size_usd": round(avg_size, 2),
            "buys": buys,
            "sells": sells,
            "categories": cat_pct,
            "top_category": max(cats, key=cats.get) if cats else "其他",
        }

        wallet_stats = self._daily_stats.setdefault(wallet, [])
        if wallet_stats and wallet_stats[-1].get("date") == today:
            wallet_stats[-1] = snap
        else:
            wallet_stats.append(snap)
        if len(wallet_stats) > 60:
            wallet_stats[:] = wallet_stats[-60:]

        self._recalc_baseline(wallet)
        self._save()

    # ------------------------------------------------------------------
    # Baseline
    # ------------------------------------------------------------------
    def _recalc_baseline(self, wallet: str):
        snaps = self._daily_stats.get(wallet, [])
        if len(snaps) < 14:
            return

        baseline_snaps = snaps[:-7]  # all except last 7 days
        if len(baseline_snaps) < 7:
            return

        avg_t = sum(s["trades"] for s in baseline_snaps) / len(baseline_snaps)
        avg_s = sum(s["avg_size_usd"] for s in baseline_snaps) / len(baseline_snaps)

        cat_acc = defaultdict(float)
        for s in baseline_snaps:
            for cat, pct in s.get("categories", {}).items():
                cat_acc[cat] += pct
        norm = sum(cat_acc.values()) or 1
        cat_dist = {k: round(v / norm, 1) for k, v in cat_acc.items()}
        top = max(cat_dist, key=cat_dist.get) if cat_dist else "其他"

        self._baseline[wallet] = {
            "avg_trades": round(avg_t, 1),
            "avg_size": round(avg_s, 2),
            "cat_dist": cat_dist,
            "top_category": top,
            "days": len(baseline_snaps),
        }

    # ------------------------------------------------------------------
    # Anomaly check
    # ------------------------------------------------------------------
    def check(self, wallet: str) -> list[str]:
        """Return alert strings if recent behavior deviates from baseline."""
        snaps = self._daily_stats.get(wallet, [])
        baseline = self._baseline.get(wallet)
        if not baseline or len(snaps) < 14:
            return []

        recent = snaps[-7:]
        r_trades = sum(s["trades"] for s in recent) / len(recent)
        r_size = sum(s["avg_size_usd"] for s in recent) / len(recent)

        r_cats = defaultdict(float)
        for s in recent:
            for cat, pct in s.get("categories", {}).items():
                r_cats[cat] += pct
        norm = sum(r_cats.values()) or 1
        r_cats = {k: round(v / norm, 1) for k, v in r_cats.items()}
        r_top = max(r_cats, key=r_cats.get) if r_cats else "其他"

        bl_t = baseline["avg_trades"]
        bl_s = baseline["avg_size"]

        alerts = []

        # 1. Frequency
        if bl_t > 0:
            chg = (r_trades - bl_t) / bl_t
            if chg > 1.0:
                alerts.append(f"交易频率暴增 {chg*100:+.0f}%（{bl_t:.1f}→{r_trades:.1f}笔/天）")
            elif chg < -0.7:
                alerts.append(f"交易频率骤降 {chg*100:+.0f}%（{bl_t:.1f}→{r_trades:.1f}笔/天）")

        # 2. Size
        if bl_s > 0:
            chg = (r_size - bl_s) / bl_s
            if abs(chg) > 1.0:
                dir_ = "增大" if chg > 0 else "缩小"
                alerts.append(f"平均交易额{dir_} {chg*100:+.0f}%（${bl_s:.0f}→${r_size:.0f}）")

        # 3. Category shift
        if baseline.get("top_category") and r_top != baseline["top_category"] and r_top != "其他":
            alerts.append(f"主要市场类型切换: {baseline['top_category']} → {r_top}")

        # 4. Buy/sell ratio
        bl_buys = sum(s["buys"] for s in snaps[:-7])
        bl_sells = sum(s["sells"] for s in snaps[:-7])
        r_buys = sum(s["buys"] for s in recent)
        r_sells = sum(s["sells"] for s in recent)
        if bl_buys + bl_sells > 0 and r_buys + r_sells > 0:
            bl_ratio = bl_buys / (bl_buys + bl_sells)
            r_ratio = r_buys / (r_buys + r_sells)
            shift = r_ratio - bl_ratio
            if abs(shift) > 0.3:
                s = "买入偏好" if shift > 0 else "卖出偏好"
                alerts.append(f"交易方向{s}偏移 {abs(shift)*100:.0f}pp（{bl_ratio*100:.0f}%→{r_ratio*100:.0f}%买入）")

        return alerts

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------
    def _save(self):
        try:
            os.makedirs(os.path.dirname(MONITOR_FILE), exist_ok=True)
            tmp = MONITOR_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({
                    "daily_stats": self._daily_stats,
                    "updated": time.time(),
                }, f)
            os.replace(tmp, MONITOR_FILE)
        except Exception:
            pass

    def _restore(self):
        try:
            if os.path.exists(MONITOR_FILE):
                with open(MONITOR_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self._daily_stats = data.get("daily_stats", {})
                for wallet in self._daily_stats:
                    try:
                        self._recalc_baseline(wallet)
                    except Exception:
                        pass
        except Exception:
            pass
