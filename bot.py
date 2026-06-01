"""
Polymarket Copy-Trading Bot
Monitors followed traders and mirrors their trades proportionally.
"""
import json, os, sys, time, datetime, argparse, urllib.request

from config import (
    FEISHU_WEBHOOK, FOLLOW_WALLETS, COPY_MULTIPLIER,
    MAX_POSITION_SIZE, MAX_DAILY_LOSS, MIN_TRADE_SIZE, USER_CAPITAL,
    MAX_POSITION_LOSS, MAX_TOTAL_EXPOSURE, MAX_PORTFOLIO_LOSS, get_wallet_config,
    STOP_LOSS_PCT, TAKE_PROFIT_PCT, MIN_VIABLE_CAPITAL,
    DAILY_REPORT_HOUR_UTC,
)
from leaderboard import (
    get_leaderboard, get_trader_profile, get_trader_value,
    get_trader_trades, get_trader_positions, get_market_info,
)
from trader import calculate_copy_size, place_copy_order, get_own_positions, \
    cancel_stale_orders, cancel_order, get_own_balance, get_liquidity_depth, \
    place_copy_market_order, place_copy_ioc_order, get_pol_balance
from trader_score_engine import TraderScoreEngine
from trader_monitor import TraderMonitor
from common.api_monitor import save_stats as save_api_stats, reset_stats as reset_api_stats

BASE = os.path.dirname(os.path.abspath(__file__))
RESULTS_DIR = os.path.join(BASE, "results")
os.makedirs(RESULTS_DIR, exist_ok=True)
PENDING_ORDERS_FILE = os.path.join(RESULTS_DIR, "pending_orders.json")
MAX_POS_FILE = os.path.join(RESULTS_DIR, "trader_max_pos.json")
TRADER_PNL_FILE = os.path.join(RESULTS_DIR, "trader_pnl.json")
ORDER_TIMEOUT_S = 300  # Cancel limit orders older than 5 minutes
CLOSED_ASSETS_FILE = os.path.join(RESULTS_DIR, "closed_assets.json")
MONTHLY_REPORT_FILE = os.path.join(RESULTS_DIR, "monthly_report_sent.txt")


from common.feishu import send_feishu as _feishu_send


def send_feishu(title, content_lines, color="blue"):
    _feishu_send(FEISHU_WEBHOOK, title, content_lines, color)


# ============================================================
# Bot Logger
# ============================================================
class BotLogger:
    def __init__(self):
        self.path = os.path.join(RESULTS_DIR, "bot_status.json")
        self.pnl_history_path = os.path.join(RESULTS_DIR, "pnl_history.json")
        self.events = []
        self.positions = []
        self.trader_scores = {}
        self.liquidity_reduced = 0
        self.pnl_history = {}
        self._save_failures = 0
        self._event_log_date = None
        self._event_log_path = os.path.join(RESULTS_DIR, "events.log")
        self._restore_state()
        self._save()

    def _restore_state(self):
        """Restore state from previous run to survive restarts."""
        try:
            if os.path.exists(self.path):
                with open(self.path, 'r', encoding='utf-8') as f:
                    prev = json.load(f)
                self.start_time = datetime.datetime.fromisoformat(
                    prev.get("started_at", datetime.datetime.now(datetime.timezone.utc).isoformat())
                ).timestamp()
                self.circuit_breaker = prev.get("circuit_breaker", False)
                self.circuit_breaker_until = prev.get("circuit_breaker_until", 0)
                self.errors = prev.get("errors", 0)
                today = prev.get("today", {})
                self._today_str = today.get("date", "")
                self.daily_loss = today.get("daily_loss", 0.0)
                self.trades_copied = today.get("trades_copied", 0)
                # Reset circuit breaker if day rolled over AND cooldown expired
                now_str = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
                if self._today_str != now_str:
                    self.daily_loss = 0.0
                    self.trades_copied = 0
                    self._today_str = now_str
                    if time.time() > self.circuit_breaker_until:
                        self.circuit_breaker = False
                self.events = prev.get("recent_events", [])[-10:]
                self.trader_scores = prev.get("trader_scores", {})
                return
        except Exception:
            pass
        self.start_time = time.time()
        self.circuit_breaker = False
        self.circuit_breaker_until = 0
        self.daily_loss = 0.0
        self._today_str = None
        self.trades_copied = 0
        self.errors = 0
        self.trader_scores = {}
        self._load_pnl_history()

    def set_positions(self, positions):
        self.positions = positions
        self._save()

    def _load_pnl_history(self):
        try:
            if os.path.exists(self.pnl_history_path):
                with open(self.pnl_history_path, 'r') as f:
                    self.pnl_history = json.load(f)
        except Exception:
            self.pnl_history = {}

    def _persist_day(self, day_str):
        if day_str not in self.pnl_history:
            self.pnl_history[day_str] = {
                'date': day_str, 'trades_copied': 0, 'daily_loss': 0, 'errors': 0,
            }
        d = self.pnl_history[day_str]
        d['trades_copied'] = self.trades_copied
        d['daily_loss'] = round(self.daily_loss, 2)
        d['errors'] = self.errors
        if len(self.pnl_history) > 90:
            oldest = sorted(self.pnl_history.keys())[0]
            del self.pnl_history[oldest]
        try:
            tmp = self.pnl_history_path + ".tmp"
            with open(tmp, 'w') as f:
                json.dump(self.pnl_history, f, ensure_ascii=False)
            os.replace(tmp, self.pnl_history_path)
        except Exception:
            pass

    def set_circuit_breaker(self):
        """Activate circuit breaker with minimum 1-hour cooldown."""
        self.circuit_breaker = True
        self.circuit_breaker_until = time.time() + 3600
        self._save()

    def maybe_roll_day(self):
        today = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%d')
        rolled = False
        if self._today_str and self._today_str != today:
            self._persist_day(self._today_str)
            rolled = True
        self._today_str = today
        return rolled

    def _state(self):
        return {
            "status": "paused" if self.circuit_breaker else "running",
            "circuit_breaker": self.circuit_breaker,
            "circuit_breaker_until": self.circuit_breaker_until,
            "started_at": datetime.datetime.fromtimestamp(self.start_time, datetime.timezone.utc).isoformat(),
            "uptime_hours": round((time.time() - self.start_time) / 3600, 1),
            "today": {"date": self._today_str or "", "trades_copied": self.trades_copied, "daily_loss": round(self.daily_loss, 2)},
            "trades_copied": self.trades_copied,
            "errors": self.errors,
            "positions": self.positions,
            "recent_events": self.events[-30:],
            "prev_balance": _prev_balance,
            "config": {
                "follow_wallets": FOLLOW_WALLETS,
                "copy_multiplier": COPY_MULTIPLIER,
                "user_capital": USER_CAPITAL,
                "max_position_size": MAX_POSITION_SIZE,
                "max_daily_loss": MAX_DAILY_LOSS,
                "max_position_loss": MAX_POSITION_LOSS,
            },
            "trader_scores": self.trader_scores,
            "liquidity_reduced": self.liquidity_reduced,
            "pnl_history": self.pnl_history,
        }

    def _save(self):
        """Atomic write to prevent readers from seeing partial data."""
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._state(), f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)  # Atomic on POSIX; best-effort on Windows
            self._save_failures = 0
        except Exception:
            self._save_failures += 1

    def log(self, event_type, message, pair=""):
        ts = datetime.datetime.now(datetime.timezone.utc).strftime("%H:%M:%S")
        self.events.append({"time": ts, "type": event_type, "pair": pair, "message": message})
        if len(self.events) > 50:
            self.events = self.events[-50:]
        # Append to persistent event log (daily rotation)
        self._append_event_log(ts, event_type, pair, message)
        self._save()

    def _append_event_log(self, ts, event_type, pair, message):
        today = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%d')
        if self._event_log_date != today:
            prev_date = self._event_log_date  # Save previous date before updating
            self._event_log_date = today
            # Rotate: rename old log with PREVIOUS day's date suffix (fix B-08)
            if os.path.exists(self._event_log_path) and prev_date:
                try:
                    rotated = self._event_log_path.replace('.log', f'.{prev_date}.log')
                    os.rename(self._event_log_path, rotated)
                except Exception:
                    pass
        try:
            entry = json.dumps({
                "time": ts, "type": event_type, "pair": pair, "message": message,
            }, ensure_ascii=False)
            with open(self._event_log_path, 'a', encoding='utf-8') as f:
                f.write(entry + '\n')
        except Exception:
            pass

    def add_trade(self, trader, title, side, size, order_id):
        self.trades_copied += 1
        self.log("trade", f"Copied {trader}: {side} {size:.0f} {title} -> {order_id}")

    def add_error(self, msg):
        self.errors += 1
        self.log("error", msg)

    def add_daily_loss(self, amount):
        self.daily_loss += amount
        now = datetime.datetime.now(datetime.timezone.utc)
        today = now.strftime("%Y-%m-%d")
        if self._today_str != today:
            self._today_str = today
            self.daily_loss = amount
            self.trades_copied = 0
        self._save()


logger = BotLogger()
_prev_balance = 0.0  # Track USDC balance for daily loss (settled PnL, not unrealized)


# ============================================================
# Helpers
# ============================================================
def _trader_short(wallet):
    """Short display name for a wallet."""
    return wallet[:10] + "..." if wallet else "???"


def _seed_known_txs(wallets):
    """Pre-load recent trade hashes to avoid re-copying after restart.
    Returns a dict (ordered, for deterministic FIFO truncation)."""
    txs = {}
    for w in wallets:
        try:
            trades = get_trader_trades(w, limit=200)
            for t in trades:
                tx = t.get("tx_hash")
                if tx:
                    txs[tx] = True
        except Exception:
            pass
    return txs


def _trim_known_txs(d, keep=2000):
    """FIFO trim: drop oldest entries, keeping the most recent `keep`."""
    excess = len(d) - keep
    for _ in range(excess):
        d.pop(next(iter(d)))


def _load_pending_orders():
    """Restore pending order tracking from disk. Also cancel any orders
    from a previous session (they are stale on restart)."""
    if not os.path.exists(PENDING_ORDERS_FILE):
        return {}
    try:
        with open(PENDING_ORDERS_FILE, 'r') as f:
            data = json.load(f)
        # Cancel all orders from previous session
        for oid in data:
            try:
                cancel_order(oid)
            except Exception:
                pass
        return {}
    except Exception:
        return {}


def _save_pending_orders(d):
    try:
        with open(PENDING_ORDERS_FILE, 'w') as f:
            json.dump(d, f)
    except Exception:
        pass


def _load_trader_max_pos():
    """Restore trader max position tracking from disk (survives restarts)."""
    try:
        if os.path.exists(MAX_POS_FILE):
            with open(MAX_POS_FILE, 'r', encoding='utf-8') as f:
                raw = json.load(f)
            return {tuple(k.split("||")): v for k, v in raw.items()}
    except Exception:
        pass
    return {}


def _save_trader_max_pos(d):
    """Persist trader max position tracking to disk."""
    try:
        tmp = MAX_POS_FILE + ".tmp"
        serializable = {"||".join(k): v for k, v in d.items()}
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(serializable, f)
        os.replace(tmp, MAX_POS_FILE)
    except Exception:
        pass

_trader_max_pos = _load_trader_max_pos()  # (wallet, asset) -> max observed position size for SELL ratio calc


def _load_closed_assets():
    """Restore intentionally-closed asset set from disk (survives restarts)."""
    try:
        if os.path.exists(CLOSED_ASSETS_FILE):
            with open(CLOSED_ASSETS_FILE, 'r') as f:
                return set(json.load(f))
    except Exception:
        pass
    return set()


def _save_closed_assets(assets):
    """Persist intentionally-closed asset set to disk."""
    try:
        tmp = CLOSED_ASSETS_FILE + ".tmp"
        with open(tmp, 'w') as f:
            json.dump(list(assets), f)
        os.replace(tmp, CLOSED_ASSETS_FILE)
    except Exception:
        pass


def _load_trader_pnl():
    """Load per-trader PnL from disk."""
    try:
        if os.path.exists(TRADER_PNL_FILE):
            with open(TRADER_PNL_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
    except Exception:
        pass
    return {}


def _save_trader_pnl(d):
    """Persist per-trader PnL to disk."""
    try:
        tmp = TRADER_PNL_FILE + ".tmp"
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(d, f)
        os.replace(tmp, TRADER_PNL_FILE)
    except Exception:
        pass


_trader_pnl = _load_trader_pnl()  # wallet -> {buys_usd, sells_usd, realized_pnl, trades}
_trader_asset_owner = {}  # asset -> wallet (which trader caused our position)
_trader_condition_cache = {}  # wallet -> set(condition_ids), refreshed every 300s
_trader_lb_pnl = {}  # wallet -> monthly PnL from leaderboard (for portfolio estimation)


# ------------------------------------------------------------------
# P4: 24h trader performance review
# ------------------------------------------------------------------
_suspended_wallets: dict[str, str] = {}  # wallet -> reason (for currently suspended traders)


def _run_perf_review():
    """Review each followed wallet's attributed PnL. Suspend negative performers.

    Runs every 24h. A wallet is suspended if:
      - Cumulative realized PnL < 0 AND at least 5 trades executed AND followed > 3 days
    Suspension sets score multiplier to 0 (via TraderScoreEngine emergency block).
    """
    global _suspended_wallets

    for wallet in list(FOLLOW_WALLETS):
        pnl_data = _trader_pnl.get(wallet)
        if not pnl_data or pnl_data.get("trades", 0) < 5:
            continue  # Not enough data yet

        realized = pnl_data.get("realized_pnl", 0)
        trades = pnl_data.get("trades", 0)
        buys = pnl_data.get("buys_usd", 0)
        sells = pnl_data.get("sells_usd", 0)

        if realized < 0 and trades >= 5:
            if wallet in _suspended_wallets:
                continue  # Already suspended

            reason = f"30天滚动收益为负: ${realized:+.2f} ({trades}笔交易)"
            score_engine.set_emergency_block(wallet, reason)
            _suspended_wallets[wallet] = reason
            logger.log("error", f"PERF REVIEW: suspending {_trader_short(wallet)} — {reason}")
            send_feishu(
                "⛔ 交易员暂停 — 绩效审核未通过",
                [f"**交易者:** {_trader_short(wallet)}",
                 f"**累计盈亏:** ${realized:+.2f}",
                 f"**交易笔数:** {trades}",
                 f"**买入总额:** ${buys:,.2f}　|　**卖出总额:** ${sells:,.2f}",
                 f"**原因:** {reason}",
                 f"已自动暂停，需手动解除或等待24h自动过期。"],
                color="red",
            )
        elif realized >= 0 and wallet in _suspended_wallets:
            # Recovered — clear suspension
            score_engine.clear_emergency_block(wallet)
            del _suspended_wallets[wallet]
            logger.log("check", f"PERF REVIEW: re-enabling {_trader_short(wallet)} — PnL recovered to ${realized:+.2f}")
            send_feishu(
                "✅ 交易员恢复 — 绩效达标",
                [f"**交易者:** {_trader_short(wallet)}",
                 f"**累计盈亏:** ${realized:+.2f}",
                 f"**交易笔数:** {trades}",
                 f"已自动恢复跟单。"],
                color="green",
            )
_condition_cache_ts = 0


def _reconcile_positions():
    """Compare trader positions with own positions. Returns list of missing
    positions that the trader holds but we don't."""
    own_pos = get_own_positions()
    own_conditions = {p.get("condition_id", ""): p for p in own_pos if p.get("condition_id")}

    missing = []
    for w in FOLLOW_WALLETS:
        try:
            trader_pos = get_trader_positions(w, size_threshold=10)
        except Exception:
            continue
        for tp in trader_pos:
            cid = tp.get("condition_id", "")
            if cid and cid not in own_conditions:
                missing.append({"trader": _trader_short(w), "trader_wallet": w,
                                "condition_id": cid,
                                "asset": tp.get("asset", ""),
                                "title": tp.get("title", "")[:40],
                                "trader_size": tp.get("size", 0),
                                "trader_value": tp.get("current_value", 0),
                                "current_price": tp.get("current_price", 0)})
    return missing


def load_bot_status():
    """Load bot_status.json. Returns dict or empty dict on failure."""
    path = os.path.join(RESULTS_DIR, "bot_status.json")
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return {}


# ============================================================
# Main Bot Loop
# ============================================================
def run_bot(dry_run=True, poll_interval=5):
    global _prev_balance
    # Restore previous balance if available (for PnL continuity across restarts)
    b = load_bot_status()
    if b and b.get("prev_balance", 0) > 0:
        _prev_balance = float(b["prev_balance"])
        if _prev_balance > 100000:  # Legacy raw units → normalize to USD
            _prev_balance /= 1e6
    known_txs = _seed_known_txs(FOLLOW_WALLETS)
    pending_orders = _load_pending_orders()
    _sl_triggered = set()  # Track position assets where stop-loss already fired
    _own_closed_assets = _load_closed_assets()  # Assets we intentionally closed (persisted)
    _correlation_alerted = set()  # Track condition_ids already alerted for concentration
    last_check = int(time.time())
    # Per-wallet trade timestamp tracking (fix B-04: sliding window omission)
    last_trade_ts = {w: int(time.time()) - 300 for w in FOLLOW_WALLETS}
    last_order_cleanup = last_check
    last_reconcile = last_check
    last_resolution_check = last_check
    last_gas_check = last_check  # POL gas monitoring
    last_perf_check = last_check  # P4: trader performance review
    last_daily_report = last_check  # daily report (defer 24h from start)
    _gas_alerted = False  # Avoid duplicate low-gas alerts
    last_score_update = 0  # Track score refresh interval
    consecutive_errors = 0

    # Initialize trader scoring engine
    score_engine = TraderScoreEngine()
    SCORE_UPDATE_INTERVAL = 14400  # Update scores every 4 hours

    # Initialize trader behavior monitor
    trader_monitor = TraderMonitor()

    # Seed lb_pnl cache for emergency block portfolio estimation
    # Priority: API leaderboard PnL > existing snapshots > position initial_value > positions current_value
    for wallet in FOLLOW_WALLETS:
        try:
            # 1) Try leaderboard API (most accurate)
            resp = safe_get(
                f"{DATA_API}/v1/leaderboard",
                params={"category": "OVERALL", "timePeriod": "MONTH", "orderBy": "PNL", "limit": 50},
                timeout=10,
            )
            for entry in resp.json():
                if entry.get("proxyWallet", "").lower() == wallet.lower():
                    pnl = float(entry.get("pnl", 0) or 0)
                    if pnl > 0:
                        _trader_lb_pnl[wallet] = pnl
                        break
        except Exception:
            pass
        if wallet not in _trader_lb_pnl or not _trader_lb_pnl[wallet]:
            # 2) Fallback: snapshot pnl (trader_score_engine)
            try:
                snaps = score_engine._snapshots.get(wallet, [])
                if snaps:
                    last_pnl = snaps[-1].get("pnl", 0)
                    if last_pnl:
                        _trader_lb_pnl[wallet] = float(last_pnl)
            except Exception:
                pass
        if wallet not in _trader_lb_pnl or not _trader_lb_pnl[wallet]:
            # 3) Fallback: total position initial_value
            try:
                tpos = get_trader_positions(wallet, size_threshold=10)
                total_init = sum(float(p.get("initial_value", 0)) for p in tpos)
                if total_init > 1000:
                    _trader_lb_pnl[wallet] = total_init
            except Exception:
                pass

    if not FOLLOW_WALLETS:
        print("ERROR: FOLLOW_WALLETS is empty. Set it in your environment.")
        print("  Use --show-leaderboard to browse top traders first.")
        return

    print("=" * 55)
    print("  Polymarket Copy-Trading Bot")
    print(f"  Mode: {'DRY RUN' if dry_run else '*** LIVE ***'}")
    print(f"  Following: {len(FOLLOW_WALLETS)} trader(s)")
    print(f"  Copy multiplier: {COPY_MULTIPLIER}x")
    print(f"  User capital: ${USER_CAPITAL:,.0f}")
    print(f"  Max position: ${MAX_POSITION_SIZE:,.0f}")
    print(f"  Min trade: ${MIN_TRADE_SIZE:,.0f}")
    print(f"  Max position loss: ${MAX_POSITION_LOSS:,.2f}")
    print(f"  Seeded known txs: {len(known_txs)}")
    print("=" * 55)

    # Display followed traders info
    trader_infos = []
    for w in FOLLOW_WALLETS:
        profile = get_trader_profile(w)
        name = profile.get("pseudonym", "") or profile.get("name", "") or w[:10]
        value = get_trader_value(w)
        trader_infos.append(f"  **{name}** — Portfolio: ${value:,.0f}")
        print(f"  {name}: portfolio=${value:,.0f}  wallet={w[:10]}...")

    if not dry_run:
        send_feishu(
            "🤖 跟单 Bot 启动",
            [
                f"**模式:** LIVE　|　**资金:** ${USER_CAPITAL:,.0f}",
                f"**跟单倍数:** {COPY_MULTIPLIER}x　|　**单笔上限:** ${MAX_POSITION_SIZE:,.0f}",
                f"**单笔止损:** ${MAX_POSITION_LOSS:.2f}　|　**日亏上限:** ${MAX_DAILY_LOSS:,.0f}",
                "",
                "**跟单对象:**",
            ] + trader_infos,
            color="blue",
        )
        print("\n  *** LIVE MODE — will execute real trades ***")
        print("  Press Ctrl+C within 5 seconds to abort...")
        time.sleep(5)

    print(f"\n  Polling every {poll_interval}s...\n")

    # ----------------------------------------------------------------
    # Daily report (nested so it can access dry_run + closure vars)
    # ----------------------------------------------------------------
    def _send_daily_report():
        now_utc = datetime.datetime.now(datetime.timezone.utc)
        today_str = now_utc.strftime("%Y-%m-%d")
        beijing_str = (now_utc + datetime.timedelta(hours=8)).strftime("%Y-%m-%d %H:%M")

        uptime_days = (time.time() - logger.start_time) / 86400

        own_p = get_own_positions()
        bal = get_own_balance()
        total_val = sum(p.get("current_value", 0) for p in own_p)
        total_pnl = sum(p.get("cash_pnl", 0) for p in own_p)
        pnl_sign = "+" if total_pnl >= 0 else ""

        lines = [
            f"**时间:** {beijing_str} 北京时间",
            f"**模式:** {'LIVE' if not dry_run else 'DRY RUN'}",
            "",
            "━━━ 资产概览 ━━━",
            f"**余额:** ${bal:,.2f}" if bal else "**余额:** N/A",
            f"**持仓市值:** ${total_val:,.2f}（{len(own_p)} 个）",
            f"**浮动盈亏:** {pnl_sign}${total_pnl:.2f}",
            "",
            "━━━ 今日活动 ━━━",
            f"**跟单:** {logger.trades_copied} 笔　|　**已实现亏损:** ${logger.daily_loss:.2f}",
            f"**错误:** {logger.errors} 次　|　**流动性降额:** {logger.liquidity_reduced} 次",
            "",
            "━━━ 各交易员 ━━━",
        ]

        for w in FOLLOW_WALLETS:
            pd_ = _trader_pnl.get(w, {})
            b_ = pd_.get("buys_usd", 0)
            s_ = pd_.get("sells_usd", 0)
            r_ = pd_.get("realized_pnl", 0)
            n_ = pd_.get("trades", 0)
            short = _trader_short(w)
            if n_ > 0:
                lines.append(f"**{short}:** {n_}笔 | 买${b_:.0f} 卖${s_:.0f} | 已实现{'$' + str(round(r_,2)) if r_ >= 0 else '-$' + str(round(abs(r_),2))}")
            else:
                lines.append(f"**{short}:** 无交易")

        lines.append("")
        lines.append("━━━ 主要持仓 ━━━")
        if own_p:
            top5 = sorted(own_p, key=lambda p: abs(p.get("cash_pnl", 0)), reverse=True)[:5]
            for p in top5:
                pnl_v = p.get("cash_pnl", 0)
                sgn = "+" if pnl_v >= 0 else ""
                title = (p.get("title") or "?")[:35]
                lines.append(f"• {title}: {sgn}${pnl_v:.2f}")
        else:
            lines.append("无持仓")

        lines.append("")
        lines.append("━━━ 状态 ━━━")

        # ---- Monitor: update snapshots & detect anomalies ----
        monitor_alerts: dict[str, list[str]] = {}
        for w in FOLLOW_WALLETS:
            try:
                recent = get_trader_trades(w, limit=200)
                if recent:
                    trader_monitor.update_daily(w, recent)
                a = trader_monitor.check(w)
                if a:
                    monitor_alerts[w] = a
            except Exception:
                pass

        if logger.circuit_breaker:
            lines.append("⚠️ 熔断触发中")
        if logger.errors >= 3:
            lines.append(f"⚠️ 今日 {logger.errors} 个错误")
        if bal and bal < USER_CAPITAL * 0.8:
            lines.append(f"⚠️ 资金回撤 >20%: ${bal:.0f}")
        for w in FOLLOW_WALLETS:
            pd_ = _trader_pnl.get(w, {})
            if pd_.get("trades", 0) >= 5 and pd_.get("realized_pnl", 0) < 0:
                lines.append(f"⚠️ {_trader_short(w)} 累计亏损 ${pd_['realized_pnl']:+.2f}")
        # Trader behavior anomaly alerts
        for w, alerts in monitor_alerts.items():
            short = _trader_short(w)
            for a in alerts:
                lines.append(f"🔍 {short}: {a}")

        has_alert = any("⚠️" in l or "🔍" in l for l in lines[-10:])
        if not has_alert:
            lines.append("✅ 无异常")

        # Separate real-time alert for anomalies (don't wait for report)
        if monitor_alerts and not dry_run:
            alert_lines = []
            for w, alerts in monitor_alerts.items():
                alert_lines.append(f"**{_trader_short(w)}**")
                for a in alerts:
                    alert_lines.append(f"  • {a}")
            send_feishu("🔍 交易员行为异动", alert_lines, color="yellow")

        send_feishu(f"每日复盘 — {today_str}", lines, color="blue")
        logger.log("check", f"Daily report sent")

    # ----------------------------------------------------------------
    # Monthly summary (30-day cumulative)
    # ----------------------------------------------------------------
    def _send_monthly_report():
        """Send a 30-day cumulative summary report via Feishu."""
        now_utc = datetime.datetime.now(datetime.timezone.utc)
        beijing_str = (now_utc + datetime.timedelta(hours=8)).strftime("%Y-%m-%d %H:%M")

        uptime_days = (time.time() - logger.start_time) / 86400

        own_p = get_own_positions()
        bal = get_own_balance()
        total_val = sum(p.get("current_value", 0) for p in own_p)
        total_pnl = sum(p.get("cash_pnl", 0) for p in own_p)
        pnl_sign = "+" if total_pnl >= 0 else ""

        pnl_hist = logger.pnl_history
        trading_days = len(pnl_hist)
        total_trades = sum(d.get("trades_copied", 0) for d in pnl_hist.values())
        total_loss = sum(d.get("daily_loss", 0) for d in pnl_hist.values())

        lines = [
            f"\U0001f4ca **30日累计报告** \U0001f4ca",
            f"**时间:** {beijing_str} 北京时间",
            "",
            "\u2501\u2501\u2501 整体表现 \u2501\u2501\u2501",
            f"**运行天数:** {int(uptime_days)} 天",
            f"**当前余额:** ${bal:,.2f}" if bal else "**当前余额:** N/A",
            f"**持仓市值:** ${total_val:,.2f}（{len(own_p)} 个）",
            f"**浮动盈亏:** {pnl_sign}${total_pnl:.2f}",
            "",
            "\u2501\u2501\u2501 统计汇总 \u2501\u2501\u2501",
            f"**交易天数:** {trading_days} 天",
            f"**总跟单笔数:** {total_trades} 笔",
            f"**累计已实现亏损:** ${total_loss:.2f}",
            f"**总错误数:** {logger.errors} 次",
        ]

        lines.append("")
        lines.append("\u2501\u2501\u2501 交易员表现 \u2501\u2501\u2501")
        for w in FOLLOW_WALLETS:
            pd_ = _trader_pnl.get(w, {})
            b_ = pd_.get("buys_usd", 0)
            s_ = pd_.get("sells_usd", 0)
            r_ = pd_.get("realized_pnl", 0)
            n_ = pd_.get("trades", 0)
            short = _trader_short(w)
            if n_ > 0:
                lines.append(f"**{short}:** {n_}笔 | 买${b_:.0f} 卖${s_:.0f} | 已实现{'$' + str(round(r_,2)) if r_ >= 0 else '-$' + str(round(abs(r_),2))}")
            else:
                lines.append(f"**{short}:** 未产生交易")

        lines.append("")
        lines.append("\u2501\u2501\u2501 当前持仓 \u2501\u2501\u2501")
        if own_p:
            for p in sorted(own_p, key=lambda x: abs(x.get("cash_pnl", 0)), reverse=True)[:10]:
                pnl_v = p.get("cash_pnl", 0)
                sgn = "+" if pnl_v >= 0 else ""
                title = (p.get("title") or "?")[:40]
                lines.append(f"\u2022 {title}: {sgn}${pnl_v:.2f}")
        else:
            lines.append("无持仓")

        try:
            send_feishu("\U0001f4ca 30日累计报告", lines, color="green")
            logger.log("check", "30日累计飞书报告已发送")
        except Exception as e:
            logger.add_error(f"30日报告飞书发送失败: {e}")
            print(f"  30-day report failed: {e}")

    while True:
        try:
            now_str = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            print(f"[{now_str} UTC] Checking...")

            # Persist previous day's PnL snapshot at midnight rollover
            yesterday = logger.maybe_roll_day()
            if yesterday:
                reset_api_stats()

            # Update own positions first (needed for all downstream checks)
            own_pos = get_own_positions()
            logger.set_positions(own_pos)

            # Unified snapshot: capture all state at cycle start for consistent decisions
            _cycle_snapshot = {
                "own_pos": list(own_pos),
                "own_assets": {p.get("asset", "") for p in own_pos},
                "own_conditions": {p.get("condition_id", "") for p in own_pos if p.get("condition_id")},
                "own_values": {p.get("asset", ""): p.get("current_value", 0) for p in own_pos},
                "total_unrealized_pnl": sum(p.get("cash_pnl", 0) for p in own_pos),
                "cycle_ts": int(time.time()),
            }

            # Check daily loss limit (includes unrealized PnL)
            total_unrealized_pnl = sum(p.get("cash_pnl", 0) for p in own_pos)
            combined_loss = logger.daily_loss + max(0, -total_unrealized_pnl)
            if combined_loss >= MAX_DAILY_LOSS and not logger.circuit_breaker:
                logger.set_circuit_breaker()
                logger.log("error", f"Combined loss limit: realized=${logger.daily_loss:.0f} unrealized=${abs(min(0,total_unrealized_pnl)):.0f}")
                send_feishu(
                    "🚨 综合亏损熔断",
                    [f"**已实现亏损:** ${logger.daily_loss:.2f}",
                     f"**未实现亏损:** ${abs(min(0, total_unrealized_pnl)):.2f}",
                     f"**综合:** ${combined_loss:.2f}　|　**上限:** ${MAX_DAILY_LOSS:,.0f}",
                     f"已暂停交易，明天 UTC 00:00 自动重置。"],
                    color="red",
                )

            if logger.circuit_breaker:
                now = datetime.datetime.now(datetime.timezone.utc)
                if now.strftime("%Y-%m-%d") != logger._today_str and time.time() > logger.circuit_breaker_until:
                    logger.circuit_breaker = False
                    logger.circuit_breaker_until = 0
                    logger.daily_loss = 0
                    send_feishu("✅ 熔断解除", ["日亏损已重置，恢复交易。"], color="green")
                else:
                    print("  CIRCUIT BREAKER active — skipping")

            # Disk full alert: if _save() has been failing
            if logger._save_failures >= 5:
                print("  WARNING: bot_status.json save failing — possible disk full!")
                logger._save_failures = 0  # Reset to avoid spamming
                send_feishu(
                    "💾 磁盘写入异常",
                    [f"**bot_status.json 连续写入失败 5 次**",
                     f"**时间:** {now_str} UTC",
                     f"请检查 VPS 磁盘空间。"],
                    color="red",
                )
            usdc_balance = get_own_balance()
            if usdc_balance is not None and usdc_balance > 0 and not logger.circuit_breaker:
                if _prev_balance > 0:
                    delta = usdc_balance - _prev_balance
                    if delta < 0:
                        logger.add_daily_loss(abs(delta))
                _prev_balance = usdc_balance

            # ---- Per-position stop-loss check ----
            for p in own_pos:
                pnl = p.get("cash_pnl", 0)
                asset = p.get("asset", "")
                # Use per-wallet stop-loss if available, else default
                owner_wallet = _trader_asset_owner.get(asset, "")
                pos_loss_limit = get_wallet_config(owner_wallet).get("max_position_loss", MAX_POSITION_LOSS)
                if pnl < -pos_loss_limit and asset and asset not in _sl_triggered:
                    _sl_triggered.add(asset)
                    _own_closed_assets.add(asset)  # Exclude from reconcile
                    _save_closed_assets(_own_closed_assets)
                    size = p.get("size", 0)
                    price = p.get("current_price", 0)
                    title = p.get("title", "")[:30]
                    print(f"  STOP-LOSS: {title} PnL=${pnl:.2f} < -${pos_loss_limit:.2f}, selling {size:.0f} sh")
                    logger.log("stop_loss", f"{title} PnL=${pnl:.2f} selling {size:.0f} sh")
                    if not dry_run:
                        # FOK→IOC→limit fallback chain for thin books
                        ok, oid, err = place_copy_market_order(asset, "SELL", size, price)
                        if not ok:
                            logger.log("stop_loss", f"FOK failed ({err[:80]}), trying IOC...")
                            ok, oid, err = place_copy_ioc_order(asset, "SELL", size, price)
                        if not ok:
                            worst_price = max(price * 0.90, 0.01)
                            logger.log("stop_loss", f"IOC failed ({err[:80]}), trying limit @ {worst_price:.4f}")
                            ok, oid, err = place_copy_order(asset, "SELL", worst_price, size)
                        if ok:
                            send_feishu(
                                "🛑 止损平仓",
                                [f"**市场:** {p.get('title', 'Unknown')[:40]}",
                                 f"**亏损:** ${abs(pnl):.2f}（超过 ${pos_loss_limit:.2f} 上限）",
                                 f"**卖出:** {size:.0f} 股 @ ${price:.4f}",
                                 f"**订单ID:** {oid}"],
                                color="red",
                            )
                        else:
                            logger.add_error(f"Stop-loss failed after 3 attempts: {err[:100]}")
                            print(f"  STOP-LOSS FAILED (FOK/IOC/LIMIT): {err[:100]}")
                    else:
                        logger.log("stop_loss", f"[DRY] would stop-loss {title} {size:.0f} sh")

            # ---- Per-position %-based stop-loss / take-profit (P2) ----
            for p in own_pos:
                pnl = p.get("cash_pnl", 0)
                asset = p.get("asset", "")
                if not asset or pnl == 0:
                    continue
                cost_basis = (p.get("size", 0) or 0) * (p.get("avg_price", 0) or 0)
                if cost_basis <= 0:
                    continue
                pnl_pct = abs(pnl) / cost_basis
                pct_display = (pnl / cost_basis) * 100

                # Take-profit: cash_pnl > TAKE_PROFIT_PCT * cost
                if pnl > 0 and pnl_pct > TAKE_PROFIT_PCT:
                    size = p.get("size", 0)
                    price = p.get("current_price", 0)
                    title = p.get("title", "")[:30]
                    print(f"  TAKE-PROFIT: {title} +{pct_display:.0f}% (${pnl:+.2f}), selling {size:.0f} sh")
                    logger.log("take_profit", f"{title} +{pct_display:.0f}% ${pnl:+.2f} selling {size:.0f} sh")
                    _own_closed_assets.add(asset)
                    _save_closed_assets(_own_closed_assets)
                    if not dry_run:
                        ok, oid, err = place_copy_market_order(asset, "SELL", size, price)
                        if not ok:
                            ok, oid, err = place_copy_ioc_order(asset, "SELL", size, price)
                        if not ok:
                            ok, oid, err = place_copy_order(asset, "SELL", size, price * 0.95)
                        if ok:
                            logger.add_trade("TAKE_PROFIT", title, "SELL", size * price, oid)
                            send_feishu(
                                f"💰 止盈 (+{pct_display:.0f}%)",
                                [f"**市场:** {title}",
                                 f"**盈利:** +${pnl:.2f} (+{pct_display:.0f}%)",
                                 f"**卖出:** {size:.0f} 股 @ ${price:.4f}"],
                                color="green",
                            )
                    else:
                        logger.log("take_profit", f"[DRY] would take-profit {title} {size:.0f} sh")

                # Stop-loss: cash_pnl < -STOP_LOSS_PCT * cost
                if pnl < 0 and pnl_pct > STOP_LOSS_PCT and asset not in _own_closed_assets:
                    _own_closed_assets.add(asset)
                    size = p.get("size", 0)
                    price = p.get("current_price", 0)
                    title = p.get("title", "")[:30]
                    print(f"  STOP-LOSS%: {title} {pct_display:.0f}% (${pnl:.2f}), selling {size:.0f} sh")
                    logger.log("stop_loss_pct", f"{title} {pct_display:.0f}% ${pnl:.2f} selling {size:.0f} sh")
                    _own_closed_assets.add(asset)
                    _save_closed_assets(_own_closed_assets)
                    if not dry_run:
                        ok, oid, err = place_copy_market_order(asset, "SELL", size, price)
                        if not ok:
                            ok, oid, err = place_copy_ioc_order(asset, "SELL", size, price)
                        if not ok:
                            worst_price = max(price * 0.90, 0.01)
                            ok, oid, err = place_copy_order(asset, "SELL", size, worst_price)
                        if ok:
                            logger.add_trade("STOP_LOSS%", title, "SELL", size * price, oid)
                            send_feishu(
                                f"🛑 止损 ({pct_display:.0f}%)",
                                [f"**市场:** {title}",
                                 f"**亏损:** ${abs(pnl):.2f} ({pct_display:.0f}%)",
                                 f"**卖出:** {size:.0f} 股 @ ${price:.4f}"],
                                color="red",
                            )
                    else:
                        logger.log("stop_loss_pct", f"[DRY] would stop-loss% {title} {size:.0f} sh")

            # Clean stale closed_assets entries (position no longer held)
            held_assets = {p.get("asset", "") for p in own_pos}
            stale_cleaned = _own_closed_assets - held_assets
            if stale_cleaned:
                _own_closed_assets = held_assets & _own_closed_assets
                _save_closed_assets(_own_closed_assets)

            # Refresh trader condition cache every 5 min (used for concentration checks)
            global _condition_cache_ts
            now_ts = int(time.time())
            if now_ts - _condition_cache_ts >= 300:
                _trader_condition_cache.clear()
                for w in FOLLOW_WALLETS:
                    try:
                        tpos = get_trader_positions(w, size_threshold=10)
                        _trader_condition_cache[w] = {tp.get("condition_id", "") for tp in tpos if tp.get("condition_id")}
                    except Exception as e:
                        logger.add_error(f"trader_condition_cache refresh: {e}")
                _condition_cache_ts = now_ts

            # Check each followed trader for new trades
            for wallet in FOLLOW_WALLETS:
                if logger.circuit_breaker:
                    break

                # Emergency block: check for single-position loss > 50% of estimated portfolio
                if not score_engine.is_blocked(wallet):
                    try:
                        tv = get_trader_value(wallet)
                        tpos = get_trader_positions(wallet, size_threshold=10)
                        total_initial = sum(float(p.get("initial_value", 0)) for p in tpos)
                        # Composite portfolio estimate: current value, visible capital, or leaderboard PnL
                        lb_pnl = _trader_lb_pnl.get(wallet, 0)
                        ref = max(tv, total_initial, lb_pnl / 3 if lb_pnl > 0 else 0)
                        if ref <= 0:
                            # Fallback: use min(capital * 2, 500) to avoid over-estimation on small capital
                            ref = min(max(USER_CAPITAL, 1) * 2, 500)
                        for tp_entry in tpos:
                            cp = tp_entry.get("cash_pnl", 0) or 0
                            if cp < -ref * 0.50:
                                reason = f"单笔亏损 ${abs(cp):.0f} > 组合估值 ${ref:.0f} 的 50%"
                                score_engine.set_emergency_block(wallet, reason)
                                logger.log("error", f"EMERGENCY BLOCK {_trader_short(wallet)}: {reason}")
                                send_feishu(
                                    "🚨 紧急熔断 — 交易者暂停跟单",
                                    [f"**交易者:** {_trader_short(wallet)}",
                                     f"**组合估值:** ${ref:,.0f}",
                                     f"**单笔亏损:** ${abs(cp):.0f}",
                                     f"**原因:** {reason}",
                                     f"已暂停跟单该交易者，需手动解除。"],
                                    color="red",
                                )
                                break
                    except Exception as e:
                        logger.add_error(f"Emergency block check: {e}")

                # B-04 fix: per-wallet timestamp instead of global sliding window
                since_ts = last_trade_ts.get(wallet, last_check - 300)
                trades = get_trader_trades(wallet, limit=1000, since_timestamp=since_ts)
                for trade in trades:
                    tx = trade.get("tx_hash")
                    if not tx or tx in known_txs:
                        continue
                    known_txs[tx] = True

                    # Track latest trade timestamp per wallet (fix B-04)
                    trade_ts = trade.get("timestamp", 0)
                    if trade_ts > last_trade_ts.get(wallet, 0):
                        last_trade_ts[wallet] = trade_ts

                    trade_price = trade.get("price", 0) or 0
                    trade_size = trade.get("size", 0) or 0
                    trade_side = trade.get("side", "")
                    trade_asset = trade.get("asset", "")
                    trade_title = trade.get("title", "")
                    trade_condition = trade.get("condition_id", "")

                    title_short = trade_title[:40] if trade_title else "Unknown"
                    ts = _trader_short(trade.get("trader", wallet))

                    # ---- SELL: trader is exiting — close matching position proportionally ----
                    if trade_side == "SELL":
                        matching = None
                        for p in own_pos:
                            if p.get("asset") == trade_asset:
                                matching = p
                                break
                        if matching and matching.get("size", 0) > 0:
                            # Calculate proportional sell ratio based on tracked max position
                            sell_ratio = 1.0  # default: full exit (safety)
                            key = (wallet, trade_asset)
                            max_pos = _trader_max_pos.get(key, 0)
                            if max_pos > 0:
                                sell_ratio = min(1.0, trade_size / max_pos)
                            else:
                                # COLD START: no tracked position yet, use conservative estimate
                                # Based on trader's position size vs our position size
                                our_size = matching.get("size", 0)
                                if trade_size > 0 and our_size > 0:
                                    # Estimate: trader's position relative to ours
                                    est_ratio = min(0.5, our_size / (trade_size * 10))
                                    sell_ratio = max(0.1, est_ratio)
                                else:
                                    sell_ratio = 0.1  # Maximum 10% sell on cold start
                                print(f"  [COLD START] estimated sell_ratio={sell_ratio:.0%} for {title_short}")
                            # Update tracked max: position decreased by trade_size
                            if key in _trader_max_pos:
                                _trader_max_pos[key] = max(0, _trader_max_pos[key] - trade_size)
                            sell_shares = matching["size"] * sell_ratio
                            sell_price = matching.get("current_price", trade_price)
                            label = f"{sell_ratio:.0%}" if sell_ratio < 1.0 else "FULL"
                            print(f"  EXIT: {ts} SELL {label} → {sell_shares:.0f} sh of {title_short}")
                            if sell_ratio >= 1.0:
                                _own_closed_assets.add(trade_asset)  # Full exit: exclude from reconcile
                                _save_closed_assets(_own_closed_assets)
                            if not dry_run:
                                ok, oid, err = place_copy_market_order(trade_asset, "SELL", sell_shares, sell_price)
                                if ok:
                                    sell_usd = sell_shares * sell_price
                                    logger.add_trade(ts, title_short, "SELL", sell_usd, oid)
                                    # Per-trader PnL attribution
                                    cost_basis = sell_shares * matching.get("avg_price", sell_price)
                                    realized = sell_usd - cost_basis
                                    if wallet not in _trader_pnl:
                                        _trader_pnl[wallet] = {"buys_usd": 0, "sells_usd": 0, "realized_pnl": 0, "trades": 0}
                                    _trader_pnl[wallet]["sells_usd"] += sell_usd
                                    _trader_pnl[wallet]["realized_pnl"] += realized
                                    _save_trader_pnl(_trader_pnl)
                                    send_feishu(
                                        "📤 跟单平仓" if sell_ratio >= 1.0 else f"📤 跟单减仓 {sell_ratio:.0%}",
                                        [f"**交易者:** {ts}",
                                         f"**市场:** {title_short}",
                                         f"**卖出:** {sell_shares:.0f} 股 @ ${sell_price:.4f}",
                                         f"**订单ID:** {oid}"],
                                        color="blue",
                                    )
                                else:
                                    logger.add_error(f"SELL copy failed: {err[:100]}")
                                    print(f"  EXIT FAILED: {err[:100]}")
                            else:
                                logger.log("trade", f"[DRY] {ts} SELL {sell_ratio:.0%} {sell_shares:.0f} sh {title_short}")
                        else:
                            print(f"  SELL ignored (no matching position): {ts} {trade_asset[:12]}...")
                        continue

                    # ---- BUY: copy trader's entry ----
                    # Filter small trades (USD value)
                    if trade_size * trade_price < MIN_TRADE_SIZE:
                        continue

                    # Check if market is resolved or near expiry
                    hours_left = None
                    if trade_condition:
                        market = get_market_info(trade_condition)
                        if market is None:
                            print(f"  [info] market info unavailable for {trade_condition[:12]}..., proceeding without expiry check")
                        elif market.get("resolved"):
                            continue
                        elif market.get("end_date"):
                            try:
                                end_dt = datetime.fromisoformat(market["end_date"].replace("Z", "+00:00"))
                                hours_left = (end_dt - datetime.now(timezone.utc)).total_seconds() / 3600
                                if hours_left < 48:
                                    print(f"  SKIP: market expires in {hours_left:.0f}h — {title_short}")
                                    continue
                            except Exception as e:
                                logger.add_error(f"Market info parse error: {e}")
                                pass
                        else:
                            # No end_date available: set conservative 7-day window, then age out
                            hours_left = 168  # 7 days conservative estimate
                            print(f"  [info] {trade_condition[:12]}... has no end_date, using 7d conservative window")

                    copy_usd = calculate_copy_size(trade_size * trade_price, wallet)

                    # Apply trader score multiplier (dynamic weight)
                    score_mult = score_engine.get_multiplier(wallet)
                    copy_usd *= score_mult

                    # Half size if market expires within 7 days
                    if hours_left is not None and hours_left < 168:
                        orig = copy_usd
                        copy_usd *= 0.5
                        print(f"  EXPIRY: {hours_left:.0f}h left, halving ${orig:.1f}→${copy_usd:.1f}")

                    if copy_usd > MAX_POSITION_SIZE:
                        copy_usd = MAX_POSITION_SIZE

                    # Aggregate exposure cap: don't exceed MAX_TOTAL_EXPOSURE of capital
                    total_open_usd = sum(p.get("current_value", 0) for p in own_pos)
                    remaining = (USER_CAPITAL * MAX_TOTAL_EXPOSURE) - total_open_usd
                    if copy_usd > remaining and remaining > 0:
                        print(f"  EXPOSURE: reducing ${copy_usd:.1f}→${remaining:.1f} (open={total_open_usd:.0f})")
                        copy_usd = remaining
                    if remaining <= 0:
                        print(f"  SKIP: exposure cap ({total_open_usd:.0f}/{USER_CAPITAL*MAX_TOTAL_EXPOSURE:.0f})")
                        continue

                    # Portfolio drawdown cap: reduce to 25% if unrealized loss exceeds limit
                    total_unrealized = sum(p.get("cash_pnl", 0) for p in own_pos)
                    if total_unrealized < -MAX_PORTFOLIO_LOSS:
                        orig = copy_usd
                        copy_usd *= 0.25
                        print(f"  DRAWDOWN: reducing ${orig:.1f}→${copy_usd:.1f} (unrealized={total_unrealized:.0f})")

                    # Concentration risk: reduce size if multiple followed traders hold same market
                    if trade_condition:
                        # Use cached trader positions (refreshed every 300s in main loop)
                        trader_count = 1  # Current trader counts
                        for w2 in FOLLOW_WALLETS:
                            if w2 == wallet:
                                continue
                            cids = _trader_condition_cache.get(w2, set())
                            if trade_condition in cids:
                                trader_count += 1
                        if trader_count >= 5:
                            copy_usd *= 0.25
                            print(f"  CONC: {trader_count} traders in same market, size→25%")
                        elif trader_count >= 4:
                            copy_usd *= 0.50
                            print(f"  CONC: {trader_count} traders in same market, size→50%")
                        elif trader_count >= 3:
                            copy_usd *= 0.75
                            print(f"  CONC: {trader_count} traders in same market, size→75%")

                    # Skip if copy amount below minimum trade size
                    if copy_usd < MIN_TRADE_SIZE:
                        print(f"  SKIP: copy ${copy_usd:.2f} < min trade ${MIN_TRADE_SIZE:.2f}")
                        continue

                    # Convert USD to shares
                    copy_shares = copy_usd / trade_price if trade_price > 0 else 0
                    if copy_shares <= 0:
                        continue

                    # Liquidity check: refuse if depth unknown or insufficient
                    depth = get_liquidity_depth(trade_asset, trade_side, levels=10)
                    if depth < 0:
                        logger.add_error(f"Liquidity check failed (API error) for {trade_asset[:12]}, skipping")
                        print(f"  SKIP: order book fetch failed for {trade_asset[:12]}")
                        continue
                    if depth == 0:
                        logger.log("liquidity", f"Zero depth for {trade_asset[:12]}, skipping")
                        print(f"  SKIP: zero liquidity for {trade_asset[:12]}")
                        continue
                    if copy_shares > depth * 0.20:
                        orig_shares = copy_shares
                        copy_shares = round(depth * 0.20, 2)
                        log_msg = f"{ts} {trade_side} size reduced {orig_shares:.1f}→{copy_shares:.1f} sh (depth={depth:.1f})"
                        logger.log("liquidity", log_msg)
                        logger.liquidity_reduced += 1
                        print(f"  LIQ: {log_msg}")
                        if copy_shares <= 0:
                            continue

                    score_info = score_engine.get_score(wallet)
                    score_tag = f"[S{score_info['score']} x{score_mult}]" if score_info['reliable'] else "[S?]"
                    print(f"  NEW: {ts} {trade_side} ${copy_usd:.2f} ({copy_shares:.1f} sh) @ ${trade_price:.4f} {score_tag} — {title_short}")

                    if dry_run:
                        logger.log("trade", f"[DRY] {ts} {trade_side} ${copy_usd:.2f} {title_short}")
                    else:
                        # Pre-trade collateral check: skip if order exceeds 95% of balance
                        if trade_side == "BUY":
                            balance = get_own_balance()
                            if balance is not None and copy_usd > balance * 0.95:
                                print(f"  SKIP: insufficient balance (need ${copy_usd:.1f}, have ${balance:.1f})")
                                continue
                        success, order_id, err = place_copy_order(
                            trade_asset, trade_side, trade_price, copy_shares
                        )
                        if success:
                            logger.add_trade(ts, title_short, trade_side, copy_usd, order_id)
                            # Track trader's max position for SELL ratio calculation
                            key = (wallet, trade_asset)
                            _trader_max_pos[key] = _trader_max_pos.get(key, 0) + trade_size
                            _save_trader_max_pos(_trader_max_pos)
                            # Per-trader PnL attribution
                            _trader_asset_owner[trade_asset] = wallet
                            if wallet not in _trader_pnl:
                                _trader_pnl[wallet] = {"buys_usd": 0, "sells_usd": 0, "realized_pnl": 0, "trades": 0}
                            _trader_pnl[wallet]["buys_usd"] += copy_usd
                            _trader_pnl[wallet]["trades"] += 1
                            _save_trader_pnl(_trader_pnl)
                            pending_orders[order_id] = {
                                "ts": time.time(), "retries": 0,
                                "asset": trade_asset, "side": trade_side,
                                "price": trade_price, "shares": copy_shares,
                            }
                            _save_pending_orders(pending_orders)
                            send_feishu(
                                "📊 跟单成交",
                                [f"**交易者:** {ts}",
                                 f"**市场:** {title_short}",
                                 f"**方向:** {trade_side}　|　**金额:** ${copy_usd:.2f}",
                                 f"**价格:** ${trade_price:.4f}",
                                 f"**订单ID:** {order_id}"],
                                color="green" if trade_side == "BUY" else "blue",
                            )
                        else:
                            logger.add_error(f"Order failed: {err[:100]}")
                            print(f"  FAILED: {err[:100]}")

                # Trim known_txs periodically (FIFO, keeps most recent)
                if len(known_txs) > 5000:
                    _trim_known_txs(known_txs)

            # ---- Correlation check: detect multiple traders buying same asset ----
            cycle_buys = {}  # asset -> [(wallet, title)]
            for event in logger.events[-20:]:
                if event.get("type") == "trade" and "BUY" in event.get("message", ""):
                    # Parse asset from message — events logged as "Copied X: BUY..."
                    pass
            # Use the tracked trades from this cycle instead
            if not logger.circuit_breaker and not dry_run:
                trader_positions_by_asset = {}
                for w in FOLLOW_WALLETS:
                    cids = _trader_condition_cache.get(w, set())
                    for cid in cids:
                        trader_positions_by_asset.setdefault(cid, []).append(_trader_short(w))
                for cid, traders in trader_positions_by_asset.items():
                    if len(traders) >= 3 and cid not in _correlation_alerted:
                        _correlation_alerted.add(cid)
                        title = cid[:16]
                        for p in own_pos:
                            if p.get("condition_id") == cid:
                                title = p.get("title", "")[:40]
                                break
                        logger.log("correlation", f"{len(traders)} traders long {title}: {', '.join(traders)}")
                        send_feishu(
                            "⚠️ 集中度警告",
                            [f"**{len(traders)} 位交易者同时持有:** {title}",
                             f"**交易者:** {', '.join(traders)}",
                             f"建议检查是否过度集中。"],
                            color="yellow",
                        )
                        break
                # Cleanup stale entries: remove cids no longer held by any trader
                all_current_cids = set(trader_positions_by_asset.keys())
                _correlation_alerted &= all_current_cids

            # Cancel stale limit orders every 60s
            now_ts = int(time.time())

            # Periodic trader score update (every 4 hours)
            if now_ts - last_score_update > SCORE_UPDATE_INTERVAL:
                last_score_update = now_ts
                for wallet in FOLLOW_WALLETS:
                    try:
                        trader_val = get_trader_value(wallet)
                        lb = get_leaderboard(limit=100)
                        rank = 0
                        lb_pnl = 0.0
                        for entry in lb:
                            if entry.get("wallet", "").lower() == wallet.lower():
                                rank = entry.get("rank", 0)
                                lb_pnl = float(entry.get("pnl", 0) or 0)
                                break
                        _trader_lb_pnl[wallet] = lb_pnl  # cache for emergency check
                        score_engine.update_snapshot(wallet, trader_val, rank, lb_pnl)
                    except Exception as e:
                        logger.add_error(f"Score update for {wallet[:10]}: {e}")
                # Log scores to bot status
                logger.trader_scores = score_engine.get_all_scores()
                logger._save()

            if pending_orders and now_ts - last_order_cleanup >= 60:
                # Save API stats alongside stale order cleanup
                save_api_stats(os.path.join(RESULTS_DIR, "api_stats.json"))
                pending_orders, cancelled = cancel_stale_orders(pending_orders, ORDER_TIMEOUT_S)
                _save_pending_orders(pending_orders)
                # Market-order fallback for stale limit orders
                for cinfo in cancelled:
                    print(f"  FALLBACK: market order for stale limit {cinfo['asset'][:12]}... "
                          f"{cinfo['side']} {cinfo['shares']:.1f} sh")
                    if not dry_run:
                        fok, fid, ferr = place_copy_market_order(
                            cinfo["asset"], cinfo["side"], cinfo["shares"], cinfo.get("price", 0))
                        if fok:
                            logger.log("trade", f"Fallback market {cinfo['side']} "
                                       f"{cinfo['shares']:.1f} sh → {fid}")
                        else:
                            logger.add_error(f"Fallback market failed: {ferr[:100]}")
                last_order_cleanup = now_ts

            # Position reconciliation every 30 min (detect missed trades)
            # Skip reconciliation if we haven't copied any trades yet (cold start)
            if not dry_run and now_ts - last_reconcile >= 1800 and logger.trades_copied > 0:
                try:
                    missing = _reconcile_positions()
                    if missing:
                        titles = [m["title"] or m["condition_id"][:12] for m in missing[:5]]
                        logger.log("reconcile", f"Missing {len(missing)} positions: {', '.join(titles)}")
                        # Auto-entry: conservative sizing (50% normal, max 15% capital)
                        auto_entered = 0
                        for m in missing:
                            if not m.get("asset") or not m.get("current_price"):
                                continue
                            if score_engine.is_blocked(m.get("trader_wallet", "")):
                                continue
                            if m.get("asset") in _own_closed_assets:
                                continue  # We intentionally closed this, don't re-enter
                            copy_usd = calculate_copy_size(
                                m["trader_size"] * m["current_price"], m["trader_wallet"])
                            copy_usd *= score_engine.get_multiplier(m["trader_wallet"])
                            copy_usd *= 0.5  # Conservative: half-size
                            copy_usd = min(copy_usd, USER_CAPITAL * 0.15)
                            # Exposure cap
                            total_open = sum(p.get("current_value", 0) for p in get_own_positions())
                            remaining = (USER_CAPITAL * MAX_TOTAL_EXPOSURE) - total_open
                            if remaining <= 0 or copy_usd > remaining:
                                copy_usd = min(copy_usd, max(remaining, 0))
                            if copy_usd < MIN_TRADE_SIZE:
                                continue
                            shares = copy_usd / m["current_price"]
                            ok, oid, err = place_copy_ioc_order(m["asset"], "BUY", shares, m["current_price"])
                            if ok:
                                auto_entered += 1
                                key = (m["trader_wallet"], m["asset"])
                                _trader_max_pos[key] = _trader_max_pos.get(key, 0) + m["trader_size"]
                                logger.add_trade(
                                    m["title"][:40], m["title"][:40], "BUY", copy_usd, oid)
                                # Per-trader PnL
                                tw = m["trader_wallet"]
                                _trader_asset_owner[m["asset"]] = tw
                                if tw not in _trader_pnl:
                                    _trader_pnl[tw] = {"buys_usd": 0, "sells_usd": 0, "realized_pnl": 0, "trades": 0}
                                _trader_pnl[tw]["buys_usd"] += copy_usd
                                _trader_pnl[tw]["trades"] += 1
                                print(f"  RECONCILE: auto-entered {m['title'][:40]} ${copy_usd:.1f}")
                            else:
                                logger.add_error(f"Reconcile entry failed: {(err or '')[:80]}")
                        if auto_entered:
                            _save_trader_max_pos(_trader_max_pos)
                            _save_trader_pnl(_trader_pnl)
                            send_feishu(
                                "🔄 对账补单",
                                [f"**自动补入 {auto_entered}/{len(missing)} 个缺失仓位**",
                                 f"补入仓位:"] +
                                [f"  • {m['title'] or m['condition_id'][:16]}（交易者: {m['trader']}）"
                                 for m in missing[:5]],
                                color="yellow",
                            )
                        else:
                            send_feishu(
                                "⚠️ 仓位对账异常",
                                [f"**发现 {len(missing)} 个缺失仓位（未自动补单）**",
                                 f"交易者持有但我们没有的仓位:"] +
                                [f"  • {m['title'] or m['condition_id'][:16]}（交易者: {m['trader']}）"
                                 for m in missing[:5]],
                                color="yellow",
                            )
                except Exception as e:
                    logger.add_error(f"Position reconciliation: {e}")
                last_reconcile = now_ts

            # Resolution cleanup: exit held positions on resolved markets
            if not dry_run and own_pos and now_ts - last_resolution_check >= 300:
                for p in own_pos:
                    cid = p.get("condition_id", "")
                    if not cid:
                        continue
                    market = get_market_info(cid)
                    if market and market.get("resolved"):
                        asset = p.get("asset", "")
                        size = p.get("size", 0)
                        title = p.get("title", "")[:40]
                        price = max(p.get("current_price", 0), 0.01)
                        # Skip if recovery amount too small to justify gas cost
                        if size * price < 0.50:
                            logger.log("resolution", f"Skipping settled {title}: ${size*price:.2f} < $0.50 min recovery")
                            continue
                        logger.log("resolution", f"Market resolved: {title}, attempting exit {size:.0f} sh")
                        ok, oid, err = place_copy_market_order(asset, "SELL", size, price)
                        if ok:
                            send_feishu("✅ 结算退出", [f"**市场:** {title}", f"**卖出:** {size:.0f} 股", f"**订单:** {oid}"])
                        else:
                            logger.add_error(f"Resolution exit failed: {title} {err[:100]}")
                last_resolution_check = now_ts

            # POL gas balance check every 6 hours
            if not dry_run and now_ts - last_gas_check >= 21600:
                pol = get_pol_balance()
                if pol is not None:
                    if pol < 0.1:
                        if not _gas_alerted:
                            _gas_alerted = True
                            send_feishu(
                                "⛽ POL Gas 不足",
                                [f"**当前余额:** {pol:.4f} POL",
                                 f"**阈值:** 0.1 POL",
                                 f"**时间:** {now_str} UTC",
                                 f"请立即充值 POL，否则订单将无法提交。"],
                                color="red",
                            )
                        logger.log("error", f"Low POL gas: {pol:.4f} POL")
                    else:
                        _gas_alerted = False  # Reset alert flag when balance recovers
                        logger.log("check", f"POL gas OK: {pol:.4f}")
                last_gas_check = now_ts

            # ---- P4: Trader performance review (every 24h) ----
            if now_ts - last_perf_check >= 86400:
                _run_perf_review()
                last_perf_check = now_ts

            # ---- Daily report (once per day at configured UTC hour) ----
            now_dt = datetime.datetime.now(datetime.timezone.utc)
            if now_dt.hour == DAILY_REPORT_HOUR_UTC and now_ts - last_daily_report > 3600:
                try:
                    _send_daily_report()
                except Exception as e:
                    logger.add_error(f"Daily report failed: {e}")
                last_daily_report = now_ts
            # ---- Monthly summary (30-day cumulative) ----
            uptime_days = (time.time() - logger.start_time) / 86400
            if uptime_days >= 30 and not os.path.exists(MONTHLY_REPORT_FILE):
                try:
                    _send_monthly_report()
                    with open(MONTHLY_REPORT_FILE, 'w') as f:
                        f.write(datetime.datetime.now(datetime.timezone.utc).isoformat())
                except Exception as e:
                    logger.add_error(f"Monthly report failed: {e}")


            last_check = int(time.time())
            consecutive_errors = max(0, consecutive_errors - 1)
            logger._save()

            time.sleep(poll_interval)

        except KeyboardInterrupt:
            print("\n  Shutting down...")
            logger.log("check", "Bot stopped")
            logger._persist_day(logger._today_str)
            break
        except Exception as e:
            consecutive_errors += 1
            delay = min(60 * (2 ** min(consecutive_errors - 1, 5)), 1800)
            logger.add_error(f"{type(e).__name__}: {e}")
            print(f"  ERROR: {e}")
            print(f"  Retry in {delay}s...")
            if consecutive_errors >= 3:
                send_feishu("⚠️ 连续错误", [f"**{consecutive_errors}次连续错误**", f"```{e}```"], color="yellow")
            time.sleep(delay)


# ============================================================
# CLI
# ============================================================
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Polymarket Copy-Trading Bot")
    parser.add_argument("--live", action="store_true", help="Enable live trading (default: dry-run)")
    parser.add_argument("--interval", type=int, default=5, help="Poll interval in seconds (default: 5)")
    parser.add_argument("--show-leaderboard", action="store_true", help="Display top traders and exit")
    args = parser.parse_args()

    if args.show_leaderboard:
        print("=" * 55)
        print("  Polymarket Leaderboard — Top 25 (Monthly PnL)")
        print("=" * 55)
        traders = get_leaderboard(limit=25)
        for t in traders:
            print(f"  #{t['rank']:<3} {t['username'][:20]:<22} PnL=${t['pnl']:>10,.0f}  Vol=${t['volume']:>10,.0f}")
        print("\n  To follow: set FOLLOW_WALLETS=0xWALLET1,0xWALLET2 in .env")
        sys.exit(0)

    dry = not args.live

    # P3: Minimum viable capital check
    if not dry and USER_CAPITAL < MIN_VIABLE_CAPITAL:
        print(f"\n  FATAL: Live trading requires at least ${MIN_VIABLE_CAPITAL:,.0f} capital.")
        print(f"  Current USER_CAPITAL=${USER_CAPITAL:,.2f}")
        print(f"  Below this threshold, fixed costs (gas, $2 min trade) make profitability impossible.")
        print(f"  Either increase capital or use dry-run mode.")
        sys.exit(1)

    run_bot(dry_run=dry, poll_interval=args.interval)
