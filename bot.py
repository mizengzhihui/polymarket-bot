
"""Polymarket Copy-Trading Bot (v1.3)
Primary: Data API polling every 20s for trade detection
Secondary: Data API polling every 30s for close monitoring
- Auto-discovers traders from leaderboard + newbie scan
- Unified scoring + cascade allocation
- Stop-loss: single position -50%
- WebSocket optional (market price monitoring only)
"""
import json, os, sys, time, logging
from datetime import datetime, timezone

# Suppress noisy SDK logs for expected 404s on closed markets

BASE = os.path.dirname(os.path.abspath(__file__))
RESULTS_DIR = os.path.join(BASE, "results")
os.makedirs(RESULTS_DIR, exist_ok=True)

from config import (
    FEISHU_WEBHOOK, USER_CAPITAL, MIN_VIABLE_CAPITAL,
    STOP_LOSS_PCT, TAKE_PROFIT_PCT, SLIPPAGE_TOLERANCE, SCORE_UPDATE_INTERVAL_HOURS,
    POLLING_INTERVAL_SEC, CLOSE_MONITOR_INTERVAL_SEC, INITIAL_FOLLOW_COUNT,
    ALLOCATION_PAUSE_THRESHOLD,
)
from leaderboard import get_all_leaderboards, scan_newbies, get_trader_trades, get_trader_value
from trader_score_engine import TraderScoreEngine
from trader import (
    get_order_book, place_copy_ioc_order, get_own_positions,
    cancel_order, poll_all_trader_buys, poll_all_trader_sells,
)
from common.feishu import send_feishu as _feishu_send

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("bot")

# State
score_engine = TraderScoreEngine()
_followed_wallets = []       # list of str addresses
_own_positions = {}           # token_id -> position dict
_last_score_update = 0
_available_capital = USER_CAPITAL
_used_capital = 0.0
_paused = False

# Files
POSITIONS_FILE = os.path.join(RESULTS_DIR, "own_positions.json")
WATCHLIST_FILE = os.path.join(RESULTS_DIR, "watchlist.json")


def send_feishu(title, content_lines, color="blue"):
    _feishu_send(FEISHU_WEBHOOK, title, content_lines, color)


def discover_traders():
    """Fetch leaderboards + newbie scan, score, select top N."""
    log.info("[Discovery] Fetching leaderboards...")
    candidates = get_all_leaderboards(top_n=20)

    log.info("[Discovery] Scanning newbies...")
    newbies = scan_newbies()
    log.info(f"[Discovery] Found {len(newbies)} newbie candidates")

    seen = set()
    pool = []
    for t in candidates + newbies:
        addr = t["address"]
        if addr not in seen:
            seen.add(addr)
            pool.append(t)

    log.info(f"[Discovery] Candidate pool: {len(pool)} traders")

    for t in pool:
        try:
            trades = get_trader_trades(t["address"], limit=100)
            value = get_trader_value(t["address"])
            # Estimate wins: use cashPnl from API if available, else count sells with higher price than buys
            wins = sum(1 for tr in trades if float(tr.get("cashPnl", 0)) > 0)
            if wins == 0 and trades:
                # Fallback: approximate win as sell trades where price > 0
                buys = {t.get("asset"): float(t.get("price", 0)) for t in trades if t.get("side") == "BUY"}
                wins = sum(1 for t in trades if t.get("side") == "SELL" and float(t.get("price", 0)) > buys.get(t.get("asset"), 0))
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

    scored = [(t, score_engine.get_score(t["address"])) for t in pool]
    scored.sort(key=lambda x: x[1]["score"], reverse=True)

    n = min(INITIAL_FOLLOW_COUNT, len(scored))
    selected = [s[0] for s in scored[:n]]
    return selected


def get_best_price(token_id, side):
    """Get best available price from order book.
    Polymarket: bids are ASCENDING (best bid = bids[-1]),
                asks are DESCENDING (best ask = asks[-1]).
    """
    try:
        book = get_order_book(token_id)
        if not book:
            return None
        if side == "BUY":
            asks = book.get("asks", [])
            if asks:
                entry = asks[-1]  # lowest ask (last in descending list)
                return float(entry.get("price", 0)) if isinstance(entry, dict) else float(entry[0])
        else:
            bids = book.get("bids", [])
            if bids:
                entry = bids[-1]  # highest bid (last in ascending list)
                return float(entry.get("price", 0)) if isinstance(entry, dict) else float(entry[0])
    except Exception:
        pass
    return None


_log_counters = {}
def _log_once(key, msg):
    if key not in _log_counters:
        _log_counters[key] = 0
    _log_counters[key] += 1
    if _log_counters[key] <= 3:
        log.info(msg)
    elif _log_counters[key] == 4:
        log.info(f"  (suppressed repeated: {key})")


def check_slippage(token_id, side, target_price):
    """Check if current market price is within slippage tolerance."""
    best = get_best_price(token_id, side)
    if best is None:
        _log_once(f"nobook_{token_id}", f"  No orderbook for {token_id[:10]}..., skipping")
        return False
    if target_price <= 0:
        return False
    slippage = abs(best - target_price) / target_price
    if slippage > SLIPPAGE_TOLERANCE:
        _log_once(f"slip_{token_id}", f"  Slippage {slippage*100:.1f}% > {SLIPPAGE_TOLERANCE*100:.0f}%, skipping")
        return False
    return True


def execute_copy(wallet, trade):
    """Execute a copy trade based on a followed trader's buy."""
    global _available_capital, _used_capital

    token_id = trade.get("asset", "")
    side = trade.get("side", "BUY")
    price = float(trade.get("price", 0))
    size = float(trade.get("size", 0))
    condition_id = trade.get("condition_id", "")

    if not token_id or not wallet:
        return

    # Already have position on this token
    if token_id in _own_positions:
        return

    # Cascade allocation
    allowed = score_engine.compute_allocation(wallet, _available_capital, _followed_wallets)
    if allowed <= 0:
        _log_once(f"noalloc_{wallet}", f"  Skip {wallet[:10]}...: no allocation left")
        return

    # Extra check: don't exceed per-wallet cap within same polling cycle
    state = score_engine._allocation_state.get(wallet, {})
    remaining = state.get("cap", 0) - state.get("used", 0)
    if remaining < 2:
        _log_once(f"cap_{wallet}", f"  Skip {wallet[:10]}...: cap remaining ${remaining:.2f} too small")
        return
    allowed = min(allowed, remaining)

    copy_size = min(allowed, size)
    if copy_size < 2:
        log.info(f"  Skip {wallet[:10]}...: ${copy_size:.2f} too small")
        return

    # Slippage check
    if not check_slippage(token_id, side, price):
        return

    # Place order
    try:
        order = place_copy_ioc_order(token_id, side, copy_size, ref_price=price)
        if order:
            # place_copy_ioc_order returns (success, order_id, error)
            raw_order_id = order[1] if isinstance(order, (list, tuple)) and len(order) > 1 else str(order)
            order_id = str(raw_order_id) if raw_order_id else ""
            if not order_id or order_id == "None" or order_id == "True" or order_id == "False":
                log.error(f"  No valid order_id returned for {token_id[:10]}... order not placed")
                return
            _own_positions[token_id] = {
                "size": copy_size, "entry_price": price,
                "trader": wallet, "opened_at": time.time(),
                "condition_id": condition_id, "order_id": order_id,
            }
            _available_capital -= copy_size
            _used_capital += copy_size
            score_engine.record_allocation_used(wallet, copy_size)
            save_positions()
            log.info(f"  Copied: ${copy_size:.2f} {side} on {token_id[:10]}...")
            send_feishu("新跟单", [
                f"跟单对象: {wallet[:10]}...",
                f"标的: {token_id[:10]}...",
                f"金额: ${copy_size:.2f} @ ${price:.4f}",
            ])
    except Exception as e:
        log.error(f"  Order failed: {e}")


def check_stop_loss():
    """Scan own positions for stop-loss triggers."""
    global _available_capital, _used_capital

    try:
        chain_positions = get_own_positions()
        chain_map = {p.get("asset", ""): p for p in chain_positions}
        for token_id, my_pos in list(_own_positions.items()):
            chain_pos = chain_map.get(token_id)
            if not chain_pos:
                log.warning("[SL] Position %s... not on chain, removing", token_id[:10])
                score_engine.release_allocation(my_pos["trader"], my_pos["size"])
                _available_capital += my_pos["size"]
                _used_capital -= my_pos["size"]
                del _own_positions[token_id]
                save_positions()
                continue
            entry = my_pos["entry_price"]
            current = float(chain_pos.get("current_price", entry))

            pnl_pct = (current - entry) / entry if entry > 0 else 0

            if pnl_pct <= -STOP_LOSS_PCT:
                log.info(f"[SL] Stopping {token_id[:10]}... PnL: {pnl_pct*100:.1f}%")
                try:
                    cancel_order(my_pos["order_id"]) if my_pos.get("order_id") else log.warning("  No order_id for %s...", token_id[:10])
                    place_copy_ioc_order(token_id, "SELL", my_pos["size"], ref_price=current)
                    trader_wallet = my_pos["trader"]
                    score_engine.record_stop_loss(trader_wallet)
                    recovered = my_pos["size"] * current
                    score_engine.release_allocation(trader_wallet, my_pos["size"])
                    _available_capital += recovered
                    _used_capital -= my_pos["size"]
                    del _own_positions[token_id]
                    save_positions()
                    send_feishu("止损", [
                        f"标的: {token_id[:10]}...",
                        f"亏损: {pnl_pct*100:.1f}%",
                        f"回收: ${recovered:.2f}",
                    ])
                except Exception as e:
                    log.error(f"[SL] Close error: {e}")

            elif pnl_pct >= TAKE_PROFIT_PCT:
                log.info(f"[TP] Taking profit {token_id[:10]}... PnL: {pnl_pct*100:.1f}%")
                try:
                    cancel_order(my_pos["order_id"]) if my_pos.get("order_id") else log.warning("  No order_id for %s...", token_id[:10])
                    place_copy_ioc_order(token_id, "SELL", my_pos["size"], ref_price=current)
                    recovered = my_pos["size"] * current
                    score_engine.release_allocation(my_pos["trader"], my_pos["size"])
                    _available_capital += recovered
                    _used_capital -= my_pos["size"]
                    del _own_positions[token_id]
                    save_positions()
                    send_feishu("止盈", [
                        f"标的: {token_id[:10]}...",
                        f"盈利: {pnl_pct*100:.1f}%",
                        f"回收: ${recovered:.2f}",
                    ])
                except Exception as e:
                    log.error(f"[TP] Close error: {e}")
    except Exception as e:
        log.error(f"[SL/TP] Scan error: {e}")


def monitor_closes():
    """Poll followed traders for sells and close matching positions."""
    if not _followed_wallets:
        return
    try:
        chain_positions = get_own_positions()
        chain_ids = {p.get("asset", "") for p in chain_positions}
    except Exception:
        chain_ids = set()
    sells = poll_all_trader_sells(_followed_wallets)
    for wallet, trades in sells.items():
        for t in trades:
            token_id = t.get("asset", "")
            if token_id in _own_positions and token_id in chain_ids and _own_positions[token_id]["trader"] == wallet:
                log.info(f"[Close] Trader {wallet[:10]}... closed {token_id[:10]}...")
                try:
                    current_price = float(t.get("price", 0))
                    my_pos = _own_positions[token_id]
                    cancel_order(my_pos["order_id"]) if my_pos.get("order_id") else log.warning("  No order_id for %s...", token_id[:10])
                    place_copy_ioc_order(token_id, "SELL", my_pos["size"], ref_price=current_price)
                    recovered = my_pos["size"] * current_price if current_price > 0 else my_pos["size"]
                    score_engine.release_allocation(wallet, my_pos["size"])
                    _available_capital += recovered
                    _used_capital -= my_pos["size"]
                    del _own_positions[token_id]
                    save_positions()
                except Exception as e:
                    log.error(f"[Close] Error: {e}")


def report_status():
    """Send periodic status to Feishu."""
    global _available_capital, _used_capital, _paused
    real_capital = _available_capital + _used_capital
    lines = [
        f"资金池: ${real_capital:.2f} (初始${USER_CAPITAL:.2f})",
        f"已用: ${_used_capital:.2f}",
        f"可用: ${_available_capital:.2f}",
        f"持仓数: {len(_own_positions)}",
        f"跟单对象: {len(_followed_wallets)}",
        f"暂停: {'是' if _paused else '否'}",
    ]
    if _own_positions:
        lines.append("---持仓---")
        for tid, pos in list(_own_positions.items())[:5]:
            lines.append(f"  {tid[:10]}... ${pos['size']:.2f} @ ${pos['entry_price']:.4f}")
    send_feishu("Bot状态", lines)


def save_positions():
    global _used_capital
    try:
        with open(POSITIONS_FILE, "w") as f:
            json.dump({"positions": _own_positions, "used_capital": _used_capital, "updated": time.time()}, f)
    except Exception:
        pass


def load_positions():
    sync_positions_with_chain()
    global _own_positions, _used_capital
    try:
        if os.path.exists(POSITIONS_FILE):
            with open(POSITIONS_FILE) as f:
                data = json.load(f)
            _own_positions = data.get("positions", {})
            _used_capital = float(data.get("used_capital", 0))
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


def sync_positions_with_chain():
    """Remove positions not found on chain (startup cleanup)."""
    global _own_positions, _used_capital, _available_capital
    try:
        chain_positions = get_own_positions()
        chain_ids = {p.get("asset", "") for p in chain_positions}
        fake = [t for t in _own_positions if t not in chain_ids]
        for tid in fake:
            log.info("[Sync] Removing fake position %s...", tid[:10])
            score_engine.release_allocation(_own_positions[tid]["trader"], _own_positions[tid]["size"])
            del _own_positions[tid]
        if fake:
            _used_capital = sum(p["size"] for p in _own_positions.values())
            _available_capital = max(0, USER_CAPITAL - _used_capital)
            save_positions()
            log.info("[Sync] Capital restored to $%.2f", _available_capital)
    except Exception as e:
        log.warning("[Sync] Position sync failed: %s", e)


def main_loop():
    global _followed_wallets, _available_capital, _used_capital, _paused

    log.info("=== Polymarket Bot v1.3 starting (polling mode) ===")
    load_positions()
    load_watchlist()
    sync_positions_with_chain()

    _available_capital = USER_CAPITAL - _used_capital
    if USER_CAPITAL < MIN_VIABLE_CAPITAL:
        log.warning(f"Capital ${USER_CAPITAL} < min ${MIN_VIABLE_CAPITAL}")

    last_discovery = 0
    last_sl_check = 0
    last_close_check = 0
    last_status = 0
    last_buy_poll = 0
    discovery_retry_count = 0

    send_feishu("Bot启动", [
        f"资金池: ${USER_CAPITAL:.2f}",
        f"跟单对象: {len(_followed_wallets)}",
        "模式: 轮询 (20s)",
    ])

    while True:
        now = time.time()

        # --- 24h discovery (with retry on first failure) ---
        if now - last_discovery > SCORE_UPDATE_INTERVAL_HOURS * 3600:
            log.info("[Cycle] Running trader discovery...")
            score_engine.reset_cycle()
            try:
                selected = discover_traders()
                if selected:
                    _followed_wallets = [t["address"] for t in selected]
                    save_watchlist()
                    send_feishu("跟单列表更新", [f"发现 {len(_followed_wallets)} 个跟单对象"])
                    discovery_retry_count = 0
                elif not _followed_wallets and discovery_retry_count < 3:
                    # Retry sooner if first discovery returned nothing
                    last_discovery = now - (SCORE_UPDATE_INTERVAL_HOURS * 3600 - 300)
                    discovery_retry_count += 1
                    log.info(f"[Cycle] Retrying discovery in 5 min (attempt {discovery_retry_count}/3)")
            except Exception as e:
                log.error(f"[Cycle] Discovery error: {e}")
            last_discovery = now

        # --- Primary: poll for buys (every POLLING_INTERVAL_SEC) ---
        if now - last_buy_poll > POLLING_INTERVAL_SEC:
            try:
                buys = poll_all_trader_buys(_followed_wallets)
                for wallet, trades in buys.items():
                    for t in trades:
                        execute_copy(wallet, t)
            except Exception:
                pass
            last_buy_poll = now

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

        # --- Capital check ---
        used_pct = _used_capital / USER_CAPITAL if USER_CAPITAL > 0 else 0
        _paused = used_pct >= ALLOCATION_PAUSE_THRESHOLD

        time.sleep(1)


def start():
    try:
        main_loop()
    except KeyboardInterrupt:
        log.info("Shutting down...")
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

