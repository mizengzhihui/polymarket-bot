"""
Polymarket Copy-Trading Bot — Config (v1.3)
Loads from .env file first, then environment variables.
"""
import os
import time
import requests
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

# ============================================================
# Identity
# ============================================================
PRIVATE_KEY = os.environ.get("POLYMARKET_PRIVATE_KEY", "")
FEISHU_WEBHOOK = os.environ.get("FEISHU_WEBHOOK", "")

# ============================================================
# Followed traders (v1.3: auto-discovery replaces hardcoded list)
# Keep for backward compat / manual override
# ============================================================
FOLLOW_WALLETS_RAW = os.environ.get("FOLLOW_WALLETS", "")
FOLLOW_WALLETS = [w.strip() for w in FOLLOW_WALLETS_RAW.split(",") if w.strip()]

# ============================================================
# Helper
# ============================================================
def _safe_float(key, default):
    raw = os.environ.get(key, str(default))
    try:
        return float(raw)
    except (TypeError, ValueError):
        return float(default)

def _safe_int(key, default):
    raw = os.environ.get(key, str(default))
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        return int(default)

# ============================================================
# Capital / Risk (existing, kept for backward compat)
# ============================================================
USER_CAPITAL = _safe_float("USER_CAPITAL", "50")
MIN_VIABLE_CAPITAL = _safe_float("MIN_VIABLE_CAPITAL", "20")
MAX_DAILY_LOSS = _safe_float("MAX_DAILY_LOSS", "20")
MAX_TOTAL_EXPOSURE = _safe_float("MAX_TOTAL_EXPOSURE", "1.2")
MAX_PORTFOLIO_LOSS = _safe_float("MAX_PORTFOLIO_LOSS", "50")

# ============================================================
# Stop-loss (v1.3: single position -50%)
# ============================================================
STOP_LOSS_PCT = _safe_float("STOP_LOSS_PCT", "0.50")       # -50%
TAKE_PROFIT_PCT = _safe_float("TAKE_PROFIT_PCT", "0.50")    # take-profit still available

# ============================================================
# Slippage tolerance (v1.3: 2%)
# ============================================================
SLIPPAGE_TOLERANCE = _safe_float("SLIPPAGE_TOLERANCE", "0.02")

# ============================================================
# Scoring (v1.3 unified formula)
# ============================================================
SCORE_UPDATE_INTERVAL_HOURS = _safe_int("SCORE_UPDATE_INTERVAL_HOURS", "24")
SCORE_STOPLOSS_DISCOUNT = _safe_float("SCORE_STOPLOSS_DISCOUNT", "0.80")  # ×0.8 per stop-loss
NEWBIE_MIN_TRADES = _safe_int("NEWBIE_MIN_TRADES", "3")
NEWBIE_MIN_WINRATE = _safe_float("NEWBIE_MIN_WINRATE", "0.70")
NEWBIE_MIN_PROFIT_USD = _safe_float("NEWBIE_MIN_PROFIT_USD", "10")
NEWBIE_LOOKBACK_DAYS = _safe_int("NEWBIE_LOOKBACK_DAYS", "7")

# ============================================================
# Leaderboard discovery (v1.3)
# ============================================================
LEADERBOARD_TOP_N = _safe_int("LEADERBOARD_TOP_N", "20")
LEADERBOARD_CATEGORIES = ["daily", "weekly", "monthly", "yearly", "all"]
INITIAL_FOLLOW_COUNT = _safe_int("INITIAL_FOLLOW_COUNT", "5")  # start with 3-5

# ============================================================
# Cascade allocation (v1.3)
# ============================================================
ALLOCATION_TIMEOUT_HOURS = _safe_float("ALLOCATION_TIMEOUT_HOURS", "24")  # release unclaimed after 24h
ALLOCATION_PAUSE_THRESHOLD = _safe_float("ALLOCATION_PAUSE_THRESHOLD", "0.80")  # pause at 80% used

# ============================================================
# WebSocket (v1.3)
# ============================================================
WS_RECONNECT_DELAY_SEC = _safe_float("WS_RECONNECT_DELAY_SEC", "2")
WS_RECONNECT_MAX_DELAY_SEC = _safe_float("WS_RECONNECT_MAX_DELAY_SEC", "60")
POLLING_INTERVAL_SEC = _safe_int("POLLING_INTERVAL_SEC", "20")   # primary polling (15-20s)  # backup polling every 5 min
CLOSE_MONITOR_INTERVAL_SEC = _safe_int("CLOSE_MONITOR_INTERVAL_SEC", "30")  # check closes every 2 min

# ============================================================
# Legacy — kept for trader.py backward compat
# ============================================================
COPY_MULTIPLIER = _safe_float("COPY_MULTIPLIER", "1.0")
DEFAULT_TRADER_CAPITAL = _safe_float("DEFAULT_TRADER_CAPITAL", "10000")
MIN_TRADE_SIZE = _safe_float("MIN_TRADE_SIZE", "2")
MAX_POSITION_PCT = _safe_float("MAX_POSITION_PCT", "0.08")
MAX_POSITION_LOSS = _safe_float("MAX_POSITION_LOSS", "4.00")

# ============================================================
# Per-wallet overrides
# ============================================================
PER_WALLET_CONFIG = {}

def get_wallet_config(wallet):
    if not wallet:
        return {}
    return PER_WALLET_CONFIG.get(wallet.lower(), {})

# ============================================================
# Polymarket API
# ============================================================
DATA_API = "https://data-api.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_HOST = "https://clob.polymarket.com"
WS_HOST = "wss://ws-subscriptions-clob.polymarket.com/ws"
CHAIN_ID = 137

RPC_URLS = [
    "https://1rpc.io/matic",
    "https://polygon-rpc.com",
    "https://rpc-mainnet.maticvigil.com",
]

DEPOSIT_WALLET_FACTORY = "0x00000000000fb5c9adea0298d729a0cb3823cc07"
DEPOSIT_WALLET_IMPLEMENTATION = "0x58ca52ebe0dadfdf531c7062e76746de4db1eb"

# ============================================================
# Retry helpers
# ============================================================
def api_retry(max_retries=3, backoff=2):
    def decorator(func):
        def wrapper(*args, **kwargs):
            last_exc = None
            for attempt in range(max_retries):
                try:
                    resp = func(*args, **kwargs)
                    if resp.status_code == 429:
                        retry_after = int(resp.headers.get(Retry-After, str(backoff ** attempt)))
                        time.sleep(min(retry_after, 30))
                        continue
                    resp.raise_for_status()
                    return resp
                except requests.exceptions.RequestException as e:
                    last_exc = e
                    if attempt < max_retries - 1:
                        delay = min(backoff ** attempt, 30)
                        time.sleep(delay)
            raise last_exc
        return wrapper
    return decorator

def safe_get(url, params=None, timeout=15, max_retries=3, **kwargs):
    last_exc = None
    for attempt in range(max_retries):
        try:
            resp = requests.get(url, params=params, timeout=timeout, **kwargs)
            if resp.status_code == 429:
                time.sleep(min(int(resp.headers.get(Retry-After, str(2 ** attempt))), 30))
                continue
            return resp
        except requests.exceptions.RequestException as e:
            last_exc = e
            if attempt < max_retries - 1:
                time.sleep(min(2 ** attempt, 30))
    raise last_exc

def safe_post(url, json=None, timeout=10, max_retries=3, **kwargs):
    last_exc = None
    for attempt in range(max_retries):
        try:
            resp = requests.post(url, json=json, timeout=timeout, **kwargs)
            if resp.status_code == 429:
                time.sleep(min(int(resp.headers.get(Retry-After, str(2 ** attempt))), 30))
                continue
            return resp
        except requests.exceptions.RequestException as e:
            last_exc = e
            if attempt < max_retries - 1:
                time.sleep(min(2 ** attempt, 30))
    raise last_exc

def compute_deposit_wallet(eoa_address: str) -> str:
    from web3 import Web3
    from eth_abi import encode
    from Crypto.Hash import keccak

    ERC1967_CONST1 = bytes.fromhex("cc3735a920a3ca505d382bbc545af43d6000803e6038573d6000fd5b3d6000f3")
    ERC1967_CONST2 = bytes.fromhex("5155f3363d3d373d3d363d7f360894a13ba1a3210667c828492db98dca3e2076")
    ERC1967_PREFIX = 0x61003d3d8160233d3973
    factory = DEPOSIT_WALLET_FACTORY
    implementation = DEPOSIT_WALLET_IMPLEMENTATION
    wallet_id = bytes(12) + bytes.fromhex(eoa_address[2:])
    args = encode(["address", "bytes32"], [factory, wallet_id])
    salt = Web3.keccak(args)
    n = len(args)
    combined = ERC1967_PREFIX + (n << 56)
    combined_bytes = combined.to_bytes(10, "big")
    init_code = (
        combined_bytes
        + bytes.fromhex(implementation[2:])
        + bytes.fromhex("6009")
        + ERC1967_CONST2
        + ERC1967_CONST1
        + args
    )
    bytecode_hash = Web3.keccak(init_code)
    k = keccak.new(digest_bits=256)
    k.update(b"\xff")
    k.update(bytes.fromhex(factory[2:]))
    k.update(salt)
    k.update(bytecode_hash)
    return Web3.to_checksum_address(k.digest()[-20:].hex())
