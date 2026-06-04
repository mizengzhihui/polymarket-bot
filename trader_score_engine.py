"""
Trader Quality Scoring Engine — dynamic copy multiplier based on performance.

Uses two data sources already available in the bot:
  1. Leaderboard rank + ROI (from Polymarket leaderboard API)
  2. Portfolio value snapshots (from get_trader_value, tracked daily)

Score components (0-100):
  - Leaderboard Score (30 pts): rank percentile on monthly leaderboard
  - 7-Day Momentum (25 pts): portfolio value change over last 7 days
  - Consistency Bonus (25 pts): fraction of days with positive PnL
  - Realized ROI (20 pts): PnL relative to portfolio value

Score → copy_multiplier mapping:
  80+: 1.5x | 65-79: 1.2x | 50-64: 1.0x | 35-49: 0.5x | <35: block

Cascade allocation: score-weighted caps, 24h release, stop-loss discount.
"""
import json
import os
import time
from datetime import datetime, timezone
from config import SCORE_STOPLOSS_DISCOUNT, ALLOCATION_TIMEOUT_HOURS


SCORE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results", "trader_scores.json")

# Minimum days of data before score is considered reliable
MIN_DAYS = 3

# Score → multiplier thresholds
SCORE_THRESHOLDS = [
    (80, 1.5),
    (65, 1.2),
    (50, 1.0),
    (35, 0.5),
    (0,  0.0),
]


class TraderScoreEngine:
    """Scores followed traders from leaderboard data + portfolio snapshots."""

    def __init__(self):
        # wallet -> [{date_utc, value, rank, pnl}]
        self._snapshots: dict[str, list[dict]] = {}
        # wallet -> {score, multiplier, components, reliable}
        self._scores: dict[str, dict] = {}
        # wallet -> reason string (emergency block in effect)
        self._emergency_blocks: dict[str, str] = {}
        # wallet -> int (consecutive stop-losses this cycle)
        self._stop_loss_count: dict[str, int] = {}
        # wallet -> {cap, used, last_open_ts} for cascade allocation
        self._allocation_state: dict[str, dict] = {}
        self._restore()

    # ------------------------------------------------------------------
    # Data input
    # ------------------------------------------------------------------
    def update_snapshot(self, wallet: str, portfolio_value: float,
                        rank: int = 0, pnl: float = 0.0):
        """Record a daily portfolio snapshot for a trader."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        if wallet not in self._snapshots:
            self._snapshots[wallet] = []

        snaps = self._snapshots[wallet]

        # Update existing today entry or append new one
        if snaps and snaps[-1].get("date") == today:
            snaps[-1] = {"date": today, "value": portfolio_value,
                         "rank": rank, "pnl": pnl}
        else:
            snaps.append({"date": today, "value": portfolio_value,
                          "rank": rank, "pnl": pnl})

        # Keep last 30 days
        if len(snaps) > 30:
            snaps[:] = snaps[-30:]

        self._recalc(wallet)
        self._save()

    # ------------------------------------------------------------------
    # Scoring logic
    # ------------------------------------------------------------------
    def _recalc(self, wallet: str):
        snaps = self._snapshots.get(wallet, [])
        if len(snaps) < MIN_DAYS:
            self._scores[wallet] = self._default_score(len(snaps))
            return

        # 1. Leaderboard Score (0-30): based on rank percentile (reduced weight)
        # B-05 fix: safe int conversion for rank (API may return as string)
        ranks = []
        for s in snaps:
            r = s.get("rank")
            if r is None or r == 0 or r == "0":
                continue
            try:
                ranks.append(int(r))
            except (ValueError, TypeError):
                continue
        if ranks:
            avg_rank = sum(ranks) / len(ranks)
            rank_score = max(0, (1.0 - (avg_rank - 1) / 200)) * 30
        else:
            rank_score = 15  # neutral

        # 2. 7-Day Momentum (0-25): portfolio value change over last 7 days
        values = [(s["date"], s["value"]) for s in snaps if s["value"] > 0]
        if len(values) >= 2:
            recent = values[-7:]  # Last 7 daily snapshots
            old_val = recent[0][1]
            new_val = recent[-1][1]
            if old_val > 0:
                change_pct = (new_val - old_val) / old_val
                # Scale: +30% = full marks, -30% = 0
                momentum_score = max(0, min(25, (change_pct + 0.30) / 0.60 * 25))
            else:
                momentum_score = 12
        else:
            momentum_score = 12

        # 3. Consistency (0-25): days with positive portfolio change
        if len(values) >= 2:
            up_days = 0
            for i in range(1, len(values)):
                if values[i][1] > values[i-1][1]:
                    up_days += 1
            consistency = up_days / (len(values) - 1)
            consistency_score = consistency * 25
        else:
            consistency_score = 12

        # 4. Realized ROI factor (0-20): NEW! Uses PnL from snapshots
        #   Positive realized PnL → higher score; negative → penalty
        pnls = [s.get("pnl", 0) for s in snaps[-14:]]  # Last 14 days
        if pnls and any(p != 0 for p in pnls):
            total_pnl = sum(pnls)
            avg_value = sum(s["value"] for s in snaps[-14:] if s["value"] > 0) / max(len(snaps), 1)
            if avg_value > 0 and total_pnl != 0:
                roi_pct = total_pnl / avg_value
                # ROI: +20% → 20 pts, 0% → 10 pts, -20% → 0 pts
                roi_score = max(0, min(20, 10 + (roi_pct / 0.20) * 10))
            else:
                roi_score = 10
        else:
            roi_score = 10

        score = round(rank_score + momentum_score + consistency_score + roi_score)
        score = max(0, min(100, score))

        # Multiplier from thresholds
        multiplier = 1.0
        for threshold, mult in SCORE_THRESHOLDS:
            if score >= threshold:
                multiplier = mult
                break

        # Conservative ramp for new traders: 0.5x→1.0x over first 7 days
        if len(snaps) < 7:
            ramp = 0.5 + 0.5 * (len(snaps) - MIN_DAYS) / (7 - MIN_DAYS)
            multiplier *= ramp

        self._scores[wallet] = {
            "score": score,
            "leaderboard_pts": round(rank_score, 1),
            "momentum_pts": round(momentum_score, 1),
            "consistency_pts": round(consistency_score, 1),
            "days": len(snaps),
            "multiplier": multiplier,
            "reliable": len(snaps) >= MIN_DAYS,
        }

    @staticmethod
    def _default_score(days: int) -> dict:
        return {
            "score": 50, "leaderboard_pts": 20, "momentum_pts": 15,
            "consistency_pts": 15, "days": days, "multiplier": 0.5,
            "reliable": False,
        }

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------
    def get_multiplier(self, wallet: str) -> float:
        block = self._emergency_blocks.get(wallet)
        if block:
            if isinstance(block, dict) and time.time() - block.get("ts", 0) > 86400:
                self._emergency_blocks.pop(wallet, None)
                self._save()
            else:
                return 0.0
        return self._scores.get(wallet, self._default_score(0))["multiplier"]

    def get_score(self, wallet: str) -> dict:
        s = dict(self._scores.get(wallet, self._default_score(0)))
        block = self._emergency_blocks.get(wallet)
        if block:
            if isinstance(block, dict):
                if time.time() - block.get("ts", 0) > 86400:  # 24h expiry
                    self._emergency_blocks.pop(wallet, None)
                    self._save()
                else:
                    s["multiplier"] = 0.0
                    s["emergency_block"] = block["reason"]
            else:
                # Legacy string format — migrate to dict with current timestamp
                self._emergency_blocks[wallet] = {"reason": block, "ts": time.time()}
                self._save()
                s["multiplier"] = 0.0
                s["emergency_block"] = block
        return s

    def get_all_scores(self) -> dict:
        return dict(self._scores)

    def set_emergency_block(self, wallet: str, reason: str):
        """Force multiplier to 0 for a trader (e.g. single trade loss > 20% portfolio).
        Block auto-expires after 24 hours."""
        self._emergency_blocks[wallet] = {"reason": reason, "ts": time.time()}
        self._save()

    def clear_emergency_block(self, wallet: str):
        """Remove emergency block for a trader."""
        self._emergency_blocks.pop(wallet, None)
        self._save()

    def is_blocked(self, wallet: str) -> bool:
        block = self._emergency_blocks.get(wallet)
        if block is None:
            return False
        if isinstance(block, dict) and time.time() - block.get("ts", 0) > 86400:
            self._emergency_blocks.pop(wallet, None)
            self._save()
            return False
        return True

    # ------------------------------------------------------------------
    # Stop-loss tracking & cycle reset
    # ------------------------------------------------------------------
    def record_stop_loss(self, wallet: str):
        """Increment stop-loss counter for scoring discount."""
        self._stop_loss_count[wallet] = self._stop_loss_count.get(wallet, 0) + 1
        self._recalc(wallet)
        self._save()

    def reset_cycle(self, wallet: str = None):
        """Reset stop-loss counters (called at 24h score update)."""
        if wallet:
            self._stop_loss_count.pop(wallet, None)
            self._recalc(wallet)
        else:
            self._stop_loss_count = {}
            for w in list(self._snapshots.keys()):
                self._recalc(w)
        self._save()

    # ------------------------------------------------------------------
    # Cascade Allocation
    # ------------------------------------------------------------------
    def compute_allocation(self, wallet: str, available_capital: float,
                           all_wallets: list[str]) -> float:
        """Compute allowed allocation for a wallet using cascade method.
        Score-weighted cap, released after ALLOCATION_TIMEOUT_HOURS of inactivity."""
        total_score = sum(self.get_score(w)["score"] for w in all_wallets) or 1
        my_score = self.get_score(wallet)["score"]

        cap = available_capital * (my_score / total_score)
        now = time.time()

        state = self._allocation_state.setdefault(
            wallet, {"cap": 0, "used": 0, "last_open_ts": 0})
        state["cap"] = cap

        # Release allocation after timeout
        if (state["last_open_ts"] > 0
                and (now - state["last_open_ts"]) > ALLOCATION_TIMEOUT_HOURS * 3600):
            state["used"] = 0

        remaining = cap - state["used"]
        return max(0, min(remaining, available_capital))

    def record_allocation_used(self, wallet: str, amount: float):
        """Track used allocation for a wallet."""
        state = self._allocation_state.setdefault(
            wallet, {"cap": 0, "used": 0, "last_open_ts": 0})
        state["used"] += amount
        state["last_open_ts"] = time.time()
        self._save()

    def release_allocation(self, wallet: str, amount: float):
        """Release allocation when a position is closed."""
        state = self._allocation_state.setdefault(
            wallet, {"cap": 0, "used": 0, "last_open_ts": 0})
        state["used"] = max(0, state["used"] - amount)
        self._save()

    # ------------------------------------------------------------------
    # Persistence (atomic write)
    # ------------------------------------------------------------------
    def _save(self):
        try:
            os.makedirs(os.path.dirname(SCORE_FILE), exist_ok=True)
            tmp = SCORE_FILE + ".tmp"
            data = {
                "snapshots": self._snapshots,
                "emergency_blocks": self._emergency_blocks,
                "stop_loss_count": self._stop_loss_count,
                "allocation_state": self._allocation_state,
                "updated": time.time(),
            }
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f)
            os.replace(tmp, SCORE_FILE)
        except Exception:
            pass

    def _restore(self):
        try:
            if os.path.exists(SCORE_FILE):
                with open(SCORE_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self._snapshots = data.get("snapshots", {})
                self._emergency_blocks = data.get("emergency_blocks", {})
                self._stop_loss_count = data.get("stop_loss_count", {})
                self._allocation_state = data.get("allocation_state", {})
                for wallet in list(self._snapshots.keys()):
                    try:
                        self._recalc(wallet)
                    except Exception:
                        pass
        except Exception:
            pass
