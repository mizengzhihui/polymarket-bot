
"""Trader Score Engine (v1.3)
Unified scoring formula: win_rate x avg_ROI x log(trades+1) x confidence
Cascade allocation: score-weighted caps, 24h release, stop-loss discount
"""
import json, os, time, math
from datetime import datetime, timezone
from config import SCORE_STOPLOSS_DISCOUNT, ALLOCATION_TIMEOUT_HOURS, ALLOCATION_PAUSE_THRESHOLD

BASE = os.path.dirname(os.path.abspath(__file__))
SCORE_FILE = os.path.join(BASE, "results", "trader_scores.json")
ALLOC_FILE = os.path.join(BASE, "results", "allocation_state.json")


class TraderScoreEngine:
    def __init__(self):
        self._snapshots = {}   # wallet -> [{date, value, rank, pnl, trades, wins}]
        self._scores = {}      # wallet -> {score, multiplier, components}
        self._stop_loss_count = {}  # wallet -> int (consecutive stop-losses this cycle)
        self._emergency_blocks = {}
        self._allocation_state = {}  # wallet -> {cap, used, last_open_ts}
        self._restore()

    def update_snapshot(self, wallet, portfolio_value=0, rank=0, pnl=0, trades=0, wins=0):
        """Record daily snapshot for scoring."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if wallet not in self._snapshots:
            self._snapshots[wallet] = []
        snaps = self._snapshots[wallet]
        entry = {"date": today, "value": portfolio_value, "rank": rank, "pnl": pnl, "trades": trades, "wins": wins}
        if snaps and snaps[-1].get("date") == today:
            snaps[-1] = entry
        else:
            snaps.append(entry)
        if len(snaps) > 60:
            snaps[:] = snaps[-60:]
        self._recalc(wallet)
        self._save()

    def _recalc(self, wallet):
        snaps = self._snapshots.get(wallet, [])
        days = len(snaps)

        if days < 3:
            self._scores[wallet] = {"score": 50, "multiplier": 0.5, "reliable": False, "days": days}
            return

        # Compute core metrics from last 30 days of data
        recent = snaps[-30:]
        total_trades = sum(s.get("trades", 0) for s in recent)
        total_wins = sum(s.get("wins", 0) for s in recent)
        win_rate = (total_wins / total_trades) if total_trades > 0 else 0

        # Average ROI from daily PnL / value
        values = [s["value"] for s in recent if s.get("value", 0) > 0]
        pnls = [s.get("pnl", 0) for s in recent]
        if values and any(v > 0 for v in values):
            avg_value = sum(values) / len(values)
            total_pnl = sum(pnls)
            avg_roi = total_pnl / avg_value if avg_value > 0 else 0
        else:
            avg_roi = 0.01

        # Activity: log(trades + 1)
        activity = math.log(max(total_trades, 1) + 1)

        # Confidence: 1 - 1/(sample+1), scales 0.75 for 3 days to near 1.0
        confidence = 1.0 - (1.0 / (days + 1))

        # Stop-loss discount
        sl_count = self._stop_loss_count.get(wallet, 0)
        sl_discount = SCORE_STOPLOSS_DISCOUNT ** sl_count

        # Unified score (range roughly 0-100)
        raw_score = max(0, win_rate * (1 + avg_roi) * activity * confidence * 50)
        score = min(100, round(raw_score * sl_discount, 1))

        # Multiplier mapping
        if score >= 80:
            mult = 1.5
        elif score >= 65:
            mult = 1.2
        elif score >= 50:
            mult = 1.0
        elif score >= 35:
            mult = 0.5
        else:
            mult = 0.0

        self._scores[wallet] = {
            "score": score,
            "multiplier": mult,
            "win_rate": round(win_rate, 3),
            "avg_roi": round(avg_roi, 4),
            "activity": round(activity, 2),
            "confidence": round(confidence, 3),
            "sl_discount": round(sl_discount, 3),
            "days": days,
            "reliable": days >= 3,
        }

    def record_stop_loss(self, wallet):
        """Increment stop-loss counter for scoring discount."""
        self._stop_loss_count[wallet] = self._stop_loss_count.get(wallet, 0) + 1
        self._recalc(wallet)
        self._save()

    def reset_cycle(self, wallet=None):
        """Reset stop-loss counters (called at 24h score update)."""
        if wallet:
            self._stop_loss_count.pop(wallet, None)
            self._recalc(wallet)
        else:
            self._stop_loss_count = {}
            for w in list(self._snapshots.keys()):
                self._recalc(w)
        self._save()

    def get_score(self, wallet):
        s = dict(self._scores.get(wallet, {"score": 50, "multiplier": 0.5, "reliable": False, "days": 0}))
        block = self._emergency_blocks.get(wallet)
        if block:
            s["multiplier"] = 0.0
            s["blocked"] = True
        return s

    def get_all_scores(self):
        return dict(self._scores)

    def set_emergency_block(self, wallet, reason):
        self._emergency_blocks[wallet] = {"reason": reason, "ts": time.time()}
        self._save()

    def clear_emergency_block(self, wallet):
        self._emergency_blocks.pop(wallet, None)
        self._save()

    # ------------------------------------------------------------------
    # Cascade Allocation
    # ------------------------------------------------------------------
    def compute_allocation(self, wallet, available_capital, all_wallets):
        """Compute allowed allocation for a wallet using cascade method."""
        total_score = sum(self.get_score(w)["score"] for w in all_wallets) or 1
        my_score = self.get_score(wallet)["score"]

        cap = available_capital * (my_score / total_score)
        now = time.time()

        state = self._allocation_state.setdefault(wallet, {"cap": 0, "used": 0, "last_open_ts": 0})
        state["cap"] = cap

        # Check 24h timeout: if wallet hasn't opened in ALLOCATION_TIMEOUT_HOURS, release
        if state["last_open_ts"] > 0 and (now - state["last_open_ts"]) > ALLOCATION_TIMEOUT_HOURS * 3600:
            state["used"] = 0

        remaining = cap - state["used"]
        return max(0, min(remaining, available_capital))

    def record_allocation_used(self, wallet, amount):
        state = self._allocation_state.setdefault(wallet, {"cap": 0, "used": 0, "last_open_ts": 0})
        state["used"] += amount
        state["last_open_ts"] = time.time()
        self._save()

    def release_allocation(self, wallet, amount):
        state = self._allocation_state.setdefault(wallet, {"cap": 0, "used": 0, "last_open_ts": 0})
        state["used"] = max(0, state["used"] - amount)
        self._save()

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------
    def _save(self):
        try:
            os.makedirs(os.path.dirname(SCORE_FILE), exist_ok=True)
            tmp = SCORE_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({
                    "snapshots": self._snapshots,
                    "stop_loss_count": self._stop_loss_count,
                    "emergency_blocks": self._emergency_blocks,
                    "allocation_state": self._allocation_state,
                    "updated": time.time(),
                }, f)
            os.replace(tmp, SCORE_FILE)
        except Exception:
            pass

    def _restore(self):
        try:
            if os.path.exists(SCORE_FILE):
                with open(SCORE_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self._snapshots = data.get("snapshots", {})
                self._stop_loss_count = data.get("stop_loss_count", {})
                self._emergency_blocks = data.get("emergency_blocks", {})
                self._allocation_state = data.get("allocation_state", {})
                for wallet in list(self._snapshots.keys()):
                    try:
                        self._recalc(wallet)
                    except Exception:
                        pass
        except Exception:
            pass
