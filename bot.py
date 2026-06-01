"""
Polymarket Copy-Trading Bot (v1.3)
Main loop: WebSocket primary + polling backup
- Auto-discovers traders from leaderboard + newbie scan
- Unified scoring + cascade allocation
- Close monitoring via Data API polling
- 24h score update cycle
- Stop-loss: single position -50%
"""
import json, os, sys, time, logging, threading
from datetime import datetime, timezone

BASE = os.path.dirname(os.path.abspath(__file__))
RESULTS_DIR = os.path.join(BASE, "results")
os.makedirs(RESULTS_DIR, exist_ok=True)

from config import (
    FEISHU_WEBHOOK, USER_CAPITAL, MIN_VIABLE_CAPITAL, MAX_DAILY_LOSS,
    STOP_LOSS_PCT, TAKE_PROFIT_PCT, SLIPPAGE_TOLERANCE, SCORE_UPDATE_INTERVAL_HOURS,
    POLLING_INTERVAL_SEC, CLOSE_MONITOR_INTERVAL_SEC, INITIAL_FOLLOW_COUNT,
    ALLOCATION_PAUSE_THRESHOLD,
)
from leaderboard import get_all_leaderboards, scan_newbies, get_trader_trades, get_trader_value
from trader_score_engine import TraderScoreEngine
from trader import (poll_trader_sells,
    get_client, calculate_copy_size, place_copy_order, place_copy_ioc_order,
    get_own_positions, get_own_balance, cancel_stale_orders, cancel_order,
    ws_start, ws_stop, ws_subscribe, poll_all_trader_sells,
)
from common.feishu import send_feishu as _feishu_send

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("bot")

# ============================================================
# State
# ============================================================
score_engine = TraderScoreEngine()
_followed_wallets = []  # Current active follow list
_own_positions = {}     # token_id -> {size, entry_price, trader, opened_at}
_last_score_update = 0
_available_capital = USER_CAPITAL
_used_capital = 0.0
_paused = False

# Files
PENDING_ORDERS_FILE = os.path.join(RESULTS_DIR, "pending_orders.json")
POSITIONS_FILE = os.path.join(RESULTS_DIR, "own_positions.json")
WATCHLIST_FILE = os.path.join(RESULTS_DIR, "watchlist.json")


def send_feishu(title, content_lines, color="blue"):
    _feishu_send(FEISHU_WEBHOOK, title, content_lines, color)


# ============================================================
# 1. Trader Discovery (runs every 24h)
# ============================================================
def discover_traders():
    """Fetch leaderboards + newbie scan, score, select top N."""
    log.info("[Discovery] Fetching leaderboards...")
    candidates = get_all_leaderboards(top_n=20)

    log.info("[Discovery] Scanning newbies...")
    newbies = scan_newbies()
    log.info(f"[Discovery] Found {len(newbies)} newbie candidates")

    # Merge and dedup
    seen = set()
    pool = []
    for t in candidates + newbies:
        addr = t["address"]
        if addr not in seen:
            seen.add(addr)
            pool.append(t)

    log.info(f"[Discovery] Candidate pool: {len(pool)} traders")

    # Score each candidate (fetch recent trades/value for stats)
    for t in pool:
        try:
            trades = get_trader_trades(t["address"], limit=100)
            value = get_trader_value(t["address"])
            wins = sum(1 for tr in trades if tr.get("side") == "SELL" and float(tr.get("price", 0)) > 0)
            score_engine.update_snapshot(
                t["address"],
                portfolio_value=value,
                rank=t.get("rank", 0),
                pnl=t.get("profit", 0),
                trades=len(trades),
                wins=wins,
            )
        except Exception:
            pass

    # Sort by score, take top N
    scored = [(t, score_engine.get_score(t["address"])) for t in pool]
    scored.sort(key=lambda x: x[1]["score"], reverse=True)

    n = min(INITIAL_FOLLOW_COUNT, len(scored))
    selected = [s[0] for s in scored[:n]]

    log.info(f"[Discovery] Selected {len(selected)} traders:")
    for s in scored[:n]:
        s_detail = s[1]
        log.info(f"  {s[0]['address'][:10]}... score={s_detail['score']} mult={s_detail['multiplier']}")

    return selected


# ============================================================
# 2. Trade execution
# ============================================================
def execute_copy(trade_data):
    """Execute a copy trade based on a followed trader's action."""
    global _available_capital, _used_capital, _paused

    wallet = trade_data.get("wallet", "")
    token_id = trade_data.get("token_id", "")
    side = trade_data.get("side", "BUY")
    price = float(trade_data.get("price", 0))
    size = float(trade_data.get("size", 0))
    condition_id = trade_data.get("condition_id", "")

    if not token_id or not wallet:
        return

    # Check if already have position on this token
    if token_id in _own_positions:
        log.info(f"  Skip: already have position on {token_id[:10]}...")
        return

    # Cascade allocation
    all_wallets = _followed_wallets
    allowed = score_engine.compute_allocation(wallet, _available_capital, all_wallets)
    if allowed <= 0:
        log.info(f"  Skip: no allocation left for {wallet[:10]}...")
        return

    # Calculate copy size
    copy_size = min(allowed, size)
    if copy_size < 2:  # Min $2
        log.info(f"  Skip: copy size ${copy_size:.2f} too small")
        return

    # Slippage check
    try:
        from trader import get_order_book
        book = get_order_book(token_id)
        if book:
            if side == "BUY":
                best_price = float(book.get("asks", [{}])[0].get("price", price)) if book.get("asks") else price
            else:
                best_price = float(book.get("bids", [{}])[0].get("price", price)) if book.get("bids") else price
            slippage = abs(best_price - price) / price if price > 0 else 0
            if slippage > SLIPPAGE_TOLERANCE:
                log.info(f"  Skip: slippage {slippage*100:.1f}% > {SLIPPAGE_TOLERANCE*100:.0f}%")
                return
    except Exception:
        pass

    # Place order via CLOB
    try:
        if side == "BUY":
            order = place_copy_ioc_order(token_id, side, copy_size, ref_price=price)
        else:
            order = place_copy_ioc_order(token_id, side, copy_size, ref_price=price)

        if order:
            _own_positions[token_id] = {
                "size": copy_size, "entry_price": price,
                "trader": wallet, "opened_at": time.time(),
                "condition_id": condition_id, "order_id": order.get("order_id", ""),
            }
            _available_capital -= copy_size
            _used_capital += copy_size
            score_engine.record_allocation_used(wallet, copy_size)
            save_positions()
            log.info(f"  Placed: ${copy_size:.2f} {side} on {token_id[:10]}...")
            send_feishu("新跟单", [
                f"跟单对象: {wallet[:10]}...",
                f"标的: {token_id[:10]}...",
                f"方向: {side}",
                f"金额: ${copy_size:.2f}",
                f"价格: ${price:.4f}",
            ])
    except Exception as e:
        log.error(f"  Order failed: {e}")


# ============================================================
# 3. Stop-loss check
# ============================================================
def check_stop_loss():
    """Scan own positions for stop-loss triggers."""
    global _available_capital, _used_capital

    try:
        positions = get_own_positions()
        for pos in positions:
            token_id = pos.get("token_id", "")
            if token_id not in _own_positions:
                continue
            my_pos = _own_positions[token_id]
            entry = my_pos["entry_price"]
            current = float(pos.get("price", entry))

            # PnL%
            pnl_pct = (current - entry) / entry if entry > 0 else 0

            if pnl_pct <= -STOP_LOSS_PCT:
                log.info(f"[SL] Stopping out {token_id[:10]}... PnL: {pnl_pct*100:.1f}%")
                try:
                    cancel_order(my_pos["order_id"])
                    place_copy_ioc_order(token_id, "SELL", my_pos["size"], ref_price=current)
                    trader_wallet = my_pos["trader"]
                    score_engine.record_stop_loss(trader_wallet)
                    score_engine.release_allocation(trader_wallet, my_pos["size"])
                    _available_capital += my_pos["size"] * current  # approx recovery
                    _used_capital -= my_pos["size"]
                    del _own_positions[token_id]
                    save_positions()
                    send_feishu("止损", [
                        f"标的: {token_id[:10]}...",
                        f"亏损: {pnl_pct*100:.1f}%",
                        f"回收: ${my_pos['size'] * current:.2f}",
                    ])
                except Exception as e:
                    log.error(f"[SL] Error closing {token_id[:10]}...: {e}")

            elif pnl_pct >= TAKE_PROFIT_PCT:
                log.info(f"[TP] Taking profit on {token_id[:10]}... PnL: {pnl_pct*100:.1f}%")
                try:
                    cancel_order(my_pos["order_id"])
                    place_copy_ioc_order(token_id, "SELL", my_pos["size"], ref_price=current)
                    score_engine.release_allocation(my_pos["trader"], my_pos["size"])
                    _available_capital += my_pos["size"] * current
                    _used_capital -= my_pos["size"]
                    del _own_positions[token_id]
                    save_positions()
                    send_feishu("止盈", [
                        f"标的: {token_id[:10]}...",
                        f"盈利: {pnl_pct*100:.1f}%",
                        f"回收: ${my_pos['size'] * current:.2f}",
                    ])
                except Exception as e:
                    log.error(f"[TP] Error closing {token_id[:10]}...: {e}")
    except Exception as e:
        log.error(f"[SL] Scan error: {e}")


# ============================================================
# 4. Close monitoring (poll Data API)
# ============================================================
def monitor_closes():
    """Check if followed traders have closed positions."""
    if not _followed_wallets:
        return

    sells = poll_all_trader_sells(_followed_wallets)
    for wallet, trades in sells.items():
        for t in trades:
            token_id = t.get("asset", "")
            side = t.get("side", "")
            if token_id in _own_positions and _own_positions[token_id]["trader"] == wallet:
                log.info(f"[Close] Trader {wallet[:10]}... closed {token_id[:10]}...")
                try:
                    current_price = float(t.get("price", 0))
                    my_pos = _own_positions[token_id]
                    cancel_order(my_pos["order_id"])
                    place_copy_ioc_order(token_id, "SELL", my_pos["size"], ref_price=current_price)
                    score_engine.release_allocation(wallet, my_pos["size"])
                    _available_capital += my_pos["size"] * current_price if current_price > 0 else my_pos["size"]
                    _used_capital -= my_pos["size"]
                    del _own_positions[token_id]
                    save_positions()
                except Exception as e:
                    log.error(f"[Close] Error: {e}")


# ============================================================
# 5. WebSocket handler
# ============================================================
def handle_ws_message(data):
    """Handle incoming WebSocket message from Polymarket."""
    try:
        event_type = data.get("type", "")
        if event_type == "trade":
            trade_data = {
                "wallet": (data.get("maker") or "").lower(),
                "token_id": data.get("asset", ""),
                "side": data.get("side", "BUY"),
                "price": data.get("price", 0),
                "size": data.get("size", 0),
                "condition_id": data.get("condition_id", ""),
            }
            if trade_data["wallet"] in _followed_wallets:
                log.info(f"[WS] Trader {trade_data['wallet'][:10]}... {trade_data['side']} {trade_data['token_id'][:10]}...")
                execute_copy(trade_data)
    except Exception:
        pass


# ============================================================
# 6. Status reporting
# ============================================================
def report_status():
    """Send periodic status to Feishu."""
    global _available_capital, _used_capital, _paused
    now = datetime.now(timezone.utc)

    lines = [
        f"资金池: ${USER_CAPITAL:.2f}",
        f"已用: ${_used_capital:.2f}",
        f"可用: ${_available_capital:.2f}",
        f"持仓数: {len(_own_positions)}",
        f"跟单对象: {len(_followed_wallets)}",
        f"暂停: {'是' if _paused else '否'}",
        f"更新时间: {now.strftime('%Y-%m-%d %H:%M')} UTC",
    ]
    if _own_positions:
        lines.append("---持仓明细---")
        for tid, pos in list(_own_positions.items())[:5]:
            lines.append(f"{tid[:10]}... ${pos['size']:.2f} @ ${pos['entry_price']:.4f}")
    send_feishu("Bot状态", lines, color="blue")


# ============================================================
# Persistence
# ============================================================
def save_positions():
    try:
        with open(POSITIONS_FILE, "w") as f:
            json.dump({"positions": _own_positions, "updated": time.time()}, f)
    except Exception:
        pass


def load_positions():
    global _own_positions
    try:
        if os.path.exists(POSITIONS_FILE):
            with open(POSITIONS_FILE) as f:
                data = json.load(f)
            _own_positions = data.get("positions", {})
    except Exception:
        pass


def save_watchlist():
    try:
        with open(WATCHLIST_FILE, "w") as f:
            json.dump({"wallets": _followed_wallets, "updated": time.time()}, f)
    except Exception:
        pass


def load_watchlist():
    global _followed_wallets
    try:
        if os.path.exists(WATCHLIST_FILE):
            with open(WATCHLIST_FILE) as f:
                data = json.load(f)
            _followed_wallets = data.get("wallets", [])
    except Exception:
        pass


# ============================================================
# Main loop
# ============================================================
def main_loop():
    global _followed_wallets, _last_score_update, _available_capital, _paused

    log.info("=== Polymarket Bot v1.3 starting ===")
    load_positions()
    load_watchlist()

    # Check capital
    _available_capital = USER_CAPITAL - _used_capital
    if USER_CAPITAL < MIN_VIABLE_CAPITAL:
        log.warning(f"Capital ${USER_CAPITAL} < min ${MIN_VIABLE_CAPITAL}, running in discovery-only mode")

    # Start WebSocket
    ws_start()
    ws_subscribe("message", handle_ws_message)

    # Timer tracking
    last_discovery = 0
    last_sl_check = 0
    last_close_check = 0
    last_status = 0
    last_poll = 0

    send_feishu("Bot启动", [
        f"资金池: ${USER_CAPITAL:.2f}",
        f"最小资金: ${MIN_VIABLE_CAPITAL}",
        "策略: v1.3",
    ])

    while True:
        now = time.time()

        # --- 24h discovery + score update ---
        if now - last_discovery > SCORE_UPDATE_INTERVAL_HOURS * 3600:
            log.info("[Cycle] Running trader discovery...")
            score_engine.reset_cycle()
            try:
                selected = discover_traders()
                if selected:
                    _followed_wallets = [t["address"] for t in selected]
                    save_watchlist()
                    send_feishu("跟单列表更新", [
                        f"发现 {len(_followed_wallets)} 个跟单对象",
                        f"时间: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC",
                    ])
            except Exception as e:
                log.error(f"[Cycle] Discovery error: {e}")
                if not _followed_wallets and USER_CAPITAL >= MIN_VIABLE_CAPITAL:
                    log.info("[Cycle] No traders discovered, waiting for next cycle...")
            last_discovery = now

        # --- Polling backup (every POLLING_INTERVAL_SEC) ---
        if now - last_poll > POLLING_INTERVAL_SEC:
            try:
                for wallet in _followed_wallets:
                    trades = poll_trader_sells(wallet)
                    if trades:
                        for t in trades:
                            handle_ws_message({
                                "type": "trade",
                                "maker": wallet,
                                "asset": t.get("asset", ""),
                                "side": t.get("side", "BUY"),
                                "price": t.get("price", 0),
                                "size": t.get("size", 0),
                                "condition_id": t.get("condition_id", ""),
                            })
            except Exception:
                pass
            last_poll = now

        # --- Stop-loss check (every 30s) ---
        if now - last_sl_check > 30:
            try:
                check_stop_loss()
            except Exception as e:
                log.error(f"[SL] Error: {e}")
            last_sl_check = now

        # --- Close monitoring (every CLOSE_MONITOR_INTERVAL_SEC) ---
        if now - last_close_check > CLOSE_MONITOR_INTERVAL_SEC:
            try:
                monitor_closes()
            except Exception as e:
                log.error(f"[Close] Error: {e}")
            last_close_check = now

        # --- Status report (every 6h) ---
        if now - last_status > 21600:
            try:
                report_status()
            except Exception:
                pass
            last_status = now

        # --- Capital pause check ---
        used_pct = _used_capital / USER_CAPITAL if USER_CAPITAL > 0 else 0
        _paused = used_pct >= ALLOCATION_PAUSE_THRESHOLD
        if _paused:
            _available_capital = max(0, USER_CAPITAL - _used_capital)

        time.sleep(5)


def start():
    try:
        main_loop()
    except KeyboardInterrupt:
        log.info("Shutting down...")
        ws_stop()
        send_feishu("Bot停止", ["Bot 已手动停止"])
    except Exception as e:
        log.exception(f"Fatal error: {e}")
        try:
            send_feishu("Bot异常", [f"错误: {str(e)}"], color="red")
        except Exception:
            pass
        raise


if __name__ == "__main__":
    start()
