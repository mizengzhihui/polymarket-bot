"""
Polymarket Copy-Trading Bot — Config
Loads from .env file first, then environment variables.
"""
import os
import time
import requests
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

# Polymarket
PRIVATE_KEY = os.environ.get("POLYMARKET_PRIVATE_KEY", "")
FEISHU_WEBHOOK = os.environ.get("FEISHU_WEBHOOK", "")

# Followed traders (comma-separated wallet addresses)
FOLLOW_WALLETS_RAW = os.environ.get("FOLLOW_WALLETS", "")
FOLLOW_WALLETS = [w.strip() for w in FOLLOW_WALLETS_RAW.split(",") if w.strip()]

# Copy trading parameters
def _safe_float(key, default):
    """Load env var as float with fallback; log warning on invalid value."""
    raw = os.environ.get(key, str(default))
    try:
        return float(raw)
    except (TypeError, ValueError):
        print(f"  [WARN] Config {key}={raw!r} is not a valid number, using default {default}")
        return float(default)


def _safe_int(key, default):
    """Load env var as int with fallback; log warning on invalid value."""
    raw = os.environ.get(key, str(default))
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        print(f"  [WARN] Config {key}={raw!r} is not a valid integer, using default {default}")
        return int(default)


COPY_MULTIPLIER = _safe_float("COPY_MULTIPLIER", "1.0")
MAX_POSITION_SIZE = _safe_float("MAX_POSITION_SIZE", "500")
MAX_DAILY_LOSS = _safe_float("MAX_DAILY_LOSS", "100")
MIN_TRADE_SIZE = _safe_float("MIN_TRADE_SIZE", "2")
MAX_POSITION_PCT = _safe_float("MAX_POSITION_PCT", "0.08")  # per-position cap as % of capital
MIN_POSITION_PCT = _safe_float("MIN_POSITION_PCT", "0.02")  # per-position floor as % of capital
USER_CAPITAL = _safe_float("USER_CAPITAL", "1000")
DEFAULT_TRADER_CAPITAL = _safe_float("DEFAULT_TRADER_CAPITAL", "10000")
MAX_POSITION_LOSS = _safe_float("MAX_POSITION_LOSS", "2.35")  # 5% of $47
MAX_TOTAL_EXPOSURE = _safe_float("MAX_TOTAL_EXPOSURE", "1.2")  # 120% of capital
MAX_PORTFOLIO_LOSS = _safe_float("MAX_PORTFOLIO_LOSS", "50")  # aggregate unrealized loss cap

# Position-level stop-loss / take-profit (% of position cost basis)
STOP_LOSS_PCT = _safe_float("STOP_LOSS_PCT", "0.30")      # exit if cash_pnl < -30% of cost
TAKE_PROFIT_PCT = _safe_float("TAKE_PROFIT_PCT", "0.50")  # exit if cash_pnl > +50% of cost

# Minimum viable capital for live trading
MIN_VIABLE_CAPITAL = _safe_float("MIN_VIABLE_CAPITAL", "500")
# Daily report time (UTC hour, default 14 = Beijing 22:00)
DAILY_REPORT_HOUR_UTC = _safe_int("DAILY_REPORT_HOUR_UTC", "14")

# Per-wallet overrides for risk parameters (wallet address → overrides)
# ChloeT1 has higher volatility (-93% max single trade), use tighter limits
PER_WALLET_CONFIG = {
    "0x9ac2536ed93f8fe8ce91d9662b03bcbb19ccbe3d": {  # ChloeT1
        "max_position_pct": 0.05,   # 5% vs default 8%
        "max_position_loss": 1.50,  # tighter stop-loss
        "copy_multiplier": 0.3,     # lower initial multiplier
    },
}


def get_wallet_config(wallet):
    """Return per-wallet overrides dict, or empty dict if none."""
    if not wallet:
        return {}
    return PER_WALLET_CONFIG.get(wallet.lower(), {})

# Polymarket API endpoints
DATA_API = "https://data-api.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_HOST = "https://clob.polymarket.com"
CHAIN_ID = 137

# RPC endpoints for Polygon (with failover)
RPC_URLS = [
    "https://1rpc.io/matic",
    "https://polygon-rpc.com",
    "https://rpc-mainnet.maticvigil.com",
]

# Deposit Wallet (Polymarket new flow — signature_type=3 / POLY_1271)
DEPOSIT_WALLET_FACTORY = "0x00000000000Fb5C9ADea0298D729A0CB3823Cc07"
DEPOSIT_WALLET_IMPLEMENTATION = "0x58CA52ebe0DadfdF531Cde7062e76746de4Db1eB"


def api_retry(max_retries=3, backoff=2):
    """Decorator for requests.get/post with exponential backoff on 429/errors."""
    def decorator(func):
        def wrapper(*args, **kwargs):
            last_exc = None
            for attempt in range(max_retries):
                try:
                    resp = func(*args, **kwargs)
                    if resp.status_code == 429:
                        retry_after = int(resp.headers.get('Retry-After', str(backoff ** attempt)))
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
    """GET request with exponential backoff retry. Returns Response or raises."""
    last_exc = None
    for attempt in range(max_retries):
        try:
            resp = requests.get(url, params=params, timeout=timeout, **kwargs)
            if resp.status_code == 429:
                retry_after = int(resp.headers.get('Retry-After', str(2 ** attempt)))
                time.sleep(min(retry_after, 30))
                continue
            return resp
        except requests.exceptions.RequestException as e:
            last_exc = e
            if attempt < max_retries - 1:
                time.sleep(min(2 ** attempt, 30))
    raise last_exc


def safe_post(url, json=None, timeout=10, max_retries=3, **kwargs):
    """POST request with exponential backoff retry. Returns Response or raises."""
    last_exc = None
    for attempt in range(max_retries):
        try:
            resp = requests.post(url, json=json, timeout=timeout, **kwargs)
            if resp.status_code == 429:
                retry_after = int(resp.headers.get('Retry-After', str(2 ** attempt)))
                time.sleep(min(retry_after, 30))
                continue
            return resp
        except requests.exceptions.RequestException as e:
            last_exc = e
            if attempt < max_retries - 1:
                time.sleep(min(2 ** attempt, 30))
    raise last_exc


def compute_deposit_wallet(eoa_address: str) -> str:
    """Compute the deterministic deposit wallet address for an EOA."""
    from web3 import Web3
    from eth_abi import encode
    from Crypto.Hash import keccak

    ERC1967_CONST1 = bytes.fromhex("cc3735a920a3ca505d382bbc545af43d6000803e6038573d6000fd5b3d6000f3")
    ERC1967_CONST2 = bytes.fromhex("5155f3363d3d373d3d363d7f360894a13ba1a3210667c828492db98dca3e2076")
    ERC1967_PREFIX = 0x61003d3d8160233d3973

    factory = Web3.to_checksum_address(DEPOSIT_WALLET_FACTORY)
    implementation = Web3.to_checksum_address(DEPOSIT_WALLET_IMPLEMENTATION)

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
