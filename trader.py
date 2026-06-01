"""
Polymarket Trader — CLOB authentication, order execution, position sizing.
"""
import sys
import json
import os
import time
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from common.rate_limit import rate_limited
from config import (
    PRIVATE_KEY, CLOB_HOST, CHAIN_ID, COPY_MULTIPLIER, USER_CAPITAL, DATA_API,
    DEFAULT_TRADER_CAPITAL, MIN_TRADE_SIZE, MAX_POSITION_PCT, get_wallet_config, compute_deposit_wallet,
    safe_post, safe_get,
)
from leaderboard import get_trader_value

# Lazy-initialized CLOB client
_client = None
_creds = None
_deposit_wallet = None

CREDS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results", "api_creds.json")


def _load_creds():
    try:
        if os.path.exists(CREDS_FILE):
            with open(CREDS_FILE, 'r') as f:
                return json.load(f)
    except Exception:
        pass
    return None


def _save_creds(creds):
    try:
        os.makedirs(os.path.dirname(CREDS_FILE), exist_ok=True)
        tmp = CREDS_FILE + ".tmp"
        # ApiCreds is a dataclass — convert to dict for JSON serialization
        if hasattr(creds, 'api_key'):
            creds_dict = {
                "api_key": creds.api_key,
                "api_secret": creds.api_secret,
                "api_passphrase": creds.api_passphrase,
            }
        else:
            creds_dict = creds
        with open(tmp, 'w') as f:
            json.dump(creds_dict, f)
        os.replace(tmp, CREDS_FILE)
    except Exception:
        pass


def get_client():
    """Get or create authenticated CLOB client (deposit wallet flow, signature_type=3).
    NOTE: Not thread-safe. Do not call concurrently from multiple threads."""
    global _client, _creds, _deposit_wallet

    if _client is not None:
        return _client

    from py_clob_client_v2 import ClobClient
    from eth_account import Account

    if not PRIVATE_KEY:
        raise RuntimeError("POLYMARKET_PRIVATE_KEY not set")

    eoa = Account.from_key(PRIVATE_KEY).address
    _deposit_wallet = compute_deposit_wallet(eoa)

    _creds = _load_creds()
    if _creds is None:
        client = ClobClient(
            host=CLOB_HOST,
            chain_id=CHAIN_ID,
            key=PRIVATE_KEY,
            signature_type=3,
            funder=_deposit_wallet,
        )
        _creds = client.create_or_derive_api_key()
        _save_creds(_creds)
    elif isinstance(_creds, dict):
        # Restore ApiCreds from persisted dict
        from py_clob_client_v2.clob_types import ApiCreds
        _creds = ApiCreds(
            api_key=_creds.get("api_key", ""),
            api_secret=_creds.get("api_secret", ""),
            api_passphrase=_creds.get("api_passphrase", ""),
        )

    _client = ClobClient(
        host=CLOB_HOST,
        chain_id=CHAIN_ID,
        key=PRIVATE_KEY,
        creds=_creds,
        signature_type=3,
        funder=_deposit_wallet,
    )
    return _client


def get_deposit_wallet():
    """Return the computed deposit wallet address (call after get_client())."""
    global _deposit_wallet
    if _deposit_wallet is None:
        get_client()
    return _deposit_wallet


def get_order_book(token_id: str) -> dict:
    """Fetch full order book for a token from CLOB.
    Returns {'bids': [...], 'asks': [...]} on success, None on failure."""
    client = get_client()
    try:
        return client.get_order_book(token_id)
    except Exception as e:
        err_str = str(e)
        if "404" in err_str or "No orderbook" in err_str:
            pass  # silent skip for closed/inactive markets
        return None


def get_liquidity_depth(token_id: str, side: str, levels: int = 10) -> float:
    """Return total size (shares) available within top N levels for a given side.
    'BUY' side checks asks (what you'd sell into), 'SELL' side checks bids.
    Returns -1 if order book fetch failed, 0 if empty, >0 for depth."""
    book = get_order_book(token_id)
    if book is None:
        return -1.0
    entries = book.get("asks" if side.upper() == "BUY" else "bids", [])
    depth = 0.0
    for entry in entries[:levels]:
        try:
            if isinstance(entry, (list, tuple)):
                depth += float(entry[1])
            elif isinstance(entry, dict):
                depth += float(entry.get("size", 0))
        except Exception:
            continue
    return depth


def calculate_copy_size(trade_size, trader_wallet):
    """
    Calculate proportional copy size based on trader's portfolio ratio.

    Formula: copy_size = capital * (trade_size / trader_value) * multiplier
    Floor:   MIN_POSITION_PCT % of capital (default 2%)
    Ceiling: max_position_pct % of capital (per-wallet, default 8%)

    Returns 0 when portfolio value is unknown (caller should skip the trade).
    """
    from config import MIN_POSITION_PCT

    trader_value = get_trader_value(trader_wallet)
    if trader_value > 0 and trade_size > 0:
        ratio = trade_size / trader_value
        wm = get_wallet_config(trader_wallet).get("copy_multiplier", COPY_MULTIPLIER)
        copy_size = USER_CAPITAL * ratio * wm
    else:
        if trader_value <= 0:
            print(f"  [calculate_copy_size] Unknown portfolio for {trader_wallet[:10]}..., skipping trade")
        return 0

    pos_pct = get_wallet_config(trader_wallet).get("max_position_pct", MAX_POSITION_PCT)
    copy_size = min(copy_size, USER_CAPITAL * pos_pct)

    # Floor: at least MIN_POSITION_PCT % of capital (but never below MIN_TRADE_SIZE)
    floor = max(USER_CAPITAL * MIN_POSITION_PCT, MIN_TRADE_SIZE)
    copy_size = max(floor, copy_size)

    return round(copy_size, 2)


def cancel_order(order_id):
    """Cancel a single order by ID. Returns True on success."""
    client = get_client()
    try:
        client.cancel_order(order_id)
        return True
    except Exception:
        return False


MAX_CANCEL_RETRIES = 5

def cancel_stale_orders(order_ids, max_age_s=300):
    """
    Cancel open orders older than max_age_s.
    order_ids: dict of {order_id: {ts, retries, asset, side, price, shares}} or legacy [ts, retries]
    Returns (still_pending_dict, cancelled_list).
    Drops orders after MAX_CANCEL_RETRIES failed cancellations.
    """
    client = get_client()
    now = time.time()
    still_pending = {}
    stale_entries = []
    cancelled = []

    for oid, entry in order_ids.items():
        if isinstance(entry, dict):
            ts = entry.get("ts", 0)
            retries = entry.get("retries", 0)
        elif isinstance(entry, (list, tuple)):
            ts = entry[0]
            retries = entry[1] if len(entry) > 1 else 0
        else:
            ts = float(entry)
            retries = 0

        if now - ts > max_age_s:
            stale_entries.append((oid, retries, entry))
        else:
            still_pending[oid] = entry

    for oid, retries, entry in stale_entries:
        try:
            client.cancel_order(oid)
            # Collect context for fallback market order
            if isinstance(entry, dict) and "asset" in entry:
                cancelled.append({
                    "asset": entry["asset"], "side": entry["side"],
                    "price": entry.get("price", 0), "shares": entry.get("shares", 0),
                })
        except Exception:
            if retries < MAX_CANCEL_RETRIES:
                if isinstance(entry, dict):
                    entry["retries"] = retries + 1
                else:
                    entry = {"ts": ts, "retries": retries + 1}
                still_pending[oid] = entry

    return still_pending, cancelled


def place_copy_order(token_id, side, price, size):
    """
    Place a copy trade as a limit order (GTC).
    Returns (success, order_id, error_message).
    """
    from py_clob_client_v2 import Side
    from py_clob_client_v2.clob_types import OrderArgsV2, PartialCreateOrderOptions, OrderType

    client = get_client()
    order_side = Side.BUY if side.upper() == "BUY" else Side.SELL

    # Pre-check: Polymarket CLOB minimum is 5 shares
    if price <= 0:
        return False, None, f"Invalid price: {price}"
    if size < 5:
        return False, None, f"Size ({size:.2f} shares) below CLOB minimum (5 shares), need >= ${price*5:.2f} at this price"

    try:
        resp = client.create_and_post_order(
            order_args=OrderArgsV2(
                token_id=token_id,
                price=price,
                size=size,
                side=order_side,
            ),
            options=PartialCreateOrderOptions(tick_size="0.01"),
            order_type=OrderType.GTC,
        )
        order_id = str(resp[0]) if isinstance(resp, (list, tuple)) else resp.get("orderID", resp.get("id", str(resp)))
        return True, order_id, None
    except Exception as e:
        return False, None, str(e)


def place_copy_market_order(token_id, side, amount, ref_price=None):
    """
    Place a copy trade as a market order (FOK).
    amount = USDC for BUY, shares for SELL.
    ref_price = current market price (used to set dynamic worst-price). If None, uses conservative defaults.
    """
    from py_clob_client_v2 import Side
    from py_clob_client_v2.clob_types import MarketOrderArgsV2, PartialCreateOrderOptions, OrderType

    client = get_client()
    is_buy = side.upper() == "BUY"
    order_side = Side.BUY if is_buy else Side.SELL

    if ref_price and ref_price > 0:
        # Dynamic worst-price: ±5% from reference
        worst_price = min(ref_price * 1.05, 0.99) if is_buy else max(ref_price * 0.95, 0.01)
    else:
        worst_price = 0.99 if is_buy else 0.01

    try:
        resp = client.create_and_post_market_order(
            order_args=MarketOrderArgsV2(
                token_id=token_id,
                amount=amount,
                side=order_side,
                price=worst_price,
            ),
            options=PartialCreateOrderOptions(tick_size="0.01"),
            order_type=OrderType.FOK,
        )
        order_id = str(resp[0]) if isinstance(resp, (list, tuple)) else resp.get("orderID", resp.get("id", str(resp)))
        return True, order_id, None
    except Exception as e:
        return False, None, str(e)


def place_copy_ioc_order(token_id, side, amount, ref_price=None):
    """
    Place a market IOC (Immediate-or-Cancel) order. Partial fill is OK.
    Used as fallback when FOK fails on thin order books.
    """
    from py_clob_client_v2 import Side
    from py_clob_client_v2.clob_types import MarketOrderArgsV2, PartialCreateOrderOptions, OrderType

    client = get_client()
    is_buy = side.upper() == "BUY"
    order_side = Side.BUY if is_buy else Side.SELL

    if ref_price and ref_price > 0:
        worst_price = min(ref_price * 1.05, 0.99) if is_buy else max(ref_price * 0.95, 0.01)
    else:
        worst_price = 0.99 if is_buy else 0.01

    try:
        resp = client.create_and_post_market_order(
            order_args=MarketOrderArgsV2(
                token_id=token_id,
                amount=amount,
                side=order_side,
                price=worst_price,
            ),
            options=PartialCreateOrderOptions(tick_size="0.01"),
            order_type=OrderType.FAK,
        )
        order_id = str(resp[0]) if isinstance(resp, (list, tuple)) else resp.get("orderID", resp.get("id", str(resp)))
        return True, order_id, None
    except Exception as e:
        return False, None, str(e)


USDC_DECIMALS = 6  # Polygon USDC has 6 decimal places


@rate_limited(max_calls_per_second=5)
def get_own_balance():
    """Get own USDC balance in USD (normalized from raw units if needed).
    Polymarket CLOB returns raw token units (6 decimals for USDC on Polygon)."""
    from py_clob_client_v2.clob_types import BalanceAllowanceParams, AssetType

    client = get_client()
    try:
        params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
        result = client.get_balance_allowance(params)
        balance = float(result.get("balance", 0))
        if balance > 100000:  # Raw units: even $0.10 = 100,000 raw
            balance /= 10 ** USDC_DECIMALS
        return balance
    except Exception:
        return None


def get_pol_balance():
    """Get POL balance of the bot's EOA wallet (for gas monitoring).
    Uses RPC_URLS failover list — tries each endpoint in order."""
    import requests
    from config import RPC_URLS
    try:
        client = get_client()
        address = client.get_address()
    except Exception:
        return None

    last_error = None
    for rpc_url in RPC_URLS:
        try:
            resp = requests.post(
                rpc_url,
                json={'jsonrpc': '2.0', 'method': 'eth_getBalance',
                      'params': [address, 'latest'], 'id': 1},
                timeout=10,
            )
            data = resp.json()
            if 'result' in data and data['result'] is not None:
                balance_wei = int(data['result'], 16)
                return balance_wei / 1e18
            last_error = f"{rpc_url}: missing result in response"
        except requests.exceptions.Timeout:
            last_error = f"{rpc_url}: timeout"
            continue
        except requests.exceptions.RequestException as e:
            last_error = f"{rpc_url}: {e}"
            continue
        except (ValueError, TypeError) as e:
            last_error = f"{rpc_url}: parse error {e}"
            continue

    if last_error:
        print(f"  [get_pol_balance] All RPC endpoints failed, last error: {last_error}")
    return None


def get_matic_balance():
    """Deprecated alias for get_pol_balance()."""
    return get_pol_balance()


_own_positions_cache = None  # (ts, positions) for API failure fallback


@rate_limited(max_calls_per_second=5)
def get_own_positions():
    """Get own open positions via Data API. Falls back to cache on API failure."""
    import time
    global _own_positions_cache
    try:
        wallet = get_client().get_address()
        resp = safe_get(
            f"{DATA_API}/positions",
            params={"user": wallet, "limit": 500},
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
            })
        # Cache successful response
        _own_positions_cache = (int(time.time()), positions)
        return positions
    except Exception:
        # API failure: fall back to cached positions if available and fresh (< 5 min old)
        if _own_positions_cache:
            cache_ts, cached = _own_positions_cache
            if int(time.time()) - cache_ts < 300:  # 5 min stale tolerance
                print(f"  [get_own_positions] API failed, using cache (age: {int(time.time()) - cache_ts}s)")
                return cached
            # Stale cache: return empty but log warning
            print(f"  [get_own_positions] API failed, cache stale, returning empty")
        return []



# ============================================================
# WebSocket (v1.3) — real-time open trade detection
# ============================================================
import threading, json as _json, time as _time, logging as _logging

_ws = None
_ws_thread = None
_ws_running = False
_ws_callbacks = {}  # event_type -> [callbacks]


def _ws_connect():
    """Connect to Polymarket WebSocket with auto-reconnect."""
    import websocket
    from config import WS_HOST, WS_RECONNECT_DELAY_SEC, WS_RECONNECT_MAX_DELAY_SEC

    def on_message(ws, message):
        try:
            data = _json.loads(message)
            for cb in _ws_callbacks.get("message", []):
                try:
                    cb(data)
                except Exception:
                    pass
        except Exception:
            pass

    def on_error(ws, error):
        _logging.warning(f"[WS] Error: {error}")

    def on_close(ws, close_status_code, close_msg):
        _logging.info(f"[WS] Closed ({close_status_code})")

    def on_open(ws):
        _logging.info("[WS] Connected")

    delay = WS_RECONNECT_DELAY_SEC
    while _ws_running:
        try:
            ws = websocket.WebSocketApp(
                WS_HOST,
                on_message=on_message,
                on_error=on_error,
                on_close=on_close,
                on_open=on_open,
            )
            ws.run_forever(ping_interval=30, ping_timeout=10)
        except Exception as e:
            _logging.warning(f"[WS] Connection failed: {e}")
        if not _ws_running:
            break
        _time.sleep(min(delay, WS_RECONNECT_MAX_DELAY_SEC))
        delay = min(delay * 1.5, WS_RECONNECT_MAX_DELAY_SEC)


def ws_start():
    """Start WebSocket in background thread."""
    global _ws_running, _ws_thread
    if _ws_running:
        return
    _ws_running = True
    _ws_thread = threading.Thread(target=_ws_connect, daemon=True)
    _ws_thread.start()
    _logging.info("[WS] Background thread started")


def ws_stop():
    """Stop WebSocket connection."""
    global _ws_running
    _ws_running = False


def ws_subscribe(event_type, callback):
    """Register a callback for an event type."""
    if event_type not in _ws_callbacks:
        _ws_callbacks[event_type] = []
    _ws_callbacks[event_type].append(callback)


# ============================================================
# Trade monitoring (v1.3) — poll Data API for trade detection
# ============================================================
_last_trade_timestamps = {}  # wallet -> last seen timestamp


def poll_trader_sells(wallet, since_timestamp=None):
    """Poll Data API for new sell trades by a trader.
    Returns list of sell trades newer than since_timestamp (or last recorded).
    """
    global _last_trade_timestamps
    from config import DATA_API, safe_get

    since = since_timestamp or _last_trade_timestamps.get(wallet, 0)
    try:
        resp = safe_get(f"{DATA_API}/trades", params={"user": wallet, "limit": 20}, timeout=10)
        if not resp or resp.status_code != 200:
            return []
        trades = resp.json()
        sells = []
        latest_ts = since
        for t in trades:
            ts = int(t.get("timestamp", 0))
            if ts > since and t.get("side") == "SELL":
                sells.append(t)
            if ts > latest_ts:
                latest_ts = ts
        if latest_ts > since:
            _last_trade_timestamps[wallet] = latest_ts
        return sells
    except Exception:
        return []


def poll_all_trader_sells(wallets):
    """Poll for sells across all tracked wallets."""
    all_sells = {}
    for w in wallets:
        sells = poll_trader_sells(w)
        if sells:
            all_sells[w] = sells
    return all_sells


def poll_trader_buys(wallet, since_timestamp=None):
    """Poll Data API for new buy trades by a trader. Returns list of buy trades."""
    global _last_trade_timestamps
    from config import DATA_API, safe_get

    since = since_timestamp or _last_trade_timestamps.get(wallet + "_buy", 0)
    try:
        resp = safe_get(f"{DATA_API}/trades", params={"user": wallet, "limit": 20}, timeout=10)
        if not resp or resp.status_code != 200:
            return []
        trades = resp.json()
        buys = []
        latest_ts = since
        for t in trades:
            ts = int(t.get("timestamp", 0))
            if ts > since and t.get("side") == "BUY":
                buys.append(t)
            if ts > latest_ts:
                latest_ts = ts
        if latest_ts > since:
            _last_trade_timestamps[wallet + "_buy"] = latest_ts
        return buys
    except Exception:
        return []


def poll_all_trader_buys(wallets):
    """Poll for buys across all tracked wallets."""
    all_buys = {}
    for w in wallets:
        buys = poll_trader_buys(w)
        if buys:
            all_buys[w] = buys
    return all_buys
