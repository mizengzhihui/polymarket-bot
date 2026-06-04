"""Poly_Copy Deep Test Suite"""
import sys, os, json, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv; load_dotenv()

RESULTS = []
_current_test = None

def t(name):
    global _current_test
    _current_test = {"name": name, "start": time.time()}
    print(f"\n  [{name}]")

def ok(msg=""):
    ms = (time.time() - _current_test["start"]) * 1000
    RESULTS.append({"name": _current_test["name"], "status": "PASS", "ms": round(ms,1)})
    tag = f" ({msg})" if msg else ""
    print(f"  [PASS] {_current_test['name']}{tag} ({ms:.0f}ms)")

def fail(err):
    ms = (time.time() - _current_test["start"]) * 1000
    RESULTS.append({"name": _current_test["name"], "status": "FAIL", "error": str(err)[:200]})
    print(f"  [FAIL] {_current_test['name']}: {str(err)[:150]}")

def check(cond, msg="assertion failed"):
    if not cond:
        raise AssertionError(msg)

print("=" * 60)
print("  Poly_Copy Deep Test Suite")
print("=" * 60)

# ===== 1. Config =====
print("\n--- [1] Config & Environment ---")

t("env vars")
webhook = os.environ.get("FEISHU_WEBHOOK", "")
pk = os.environ.get("POLYMARKET_PRIVATE_KEY", "")
print(f"      FEISHU_WEBHOOK: {'SET' if webhook else 'MISSING'}")
print(f"      PRIVATE_KEY: {'SET' if pk else 'MISSING'}")
check(webhook, "FEISHU_WEBHOOK not set")
check(pk, "PRIVATE_KEY not set")
ok()

from config import *

t("config values")
print(f"      USER_CAPITAL=${USER_CAPITAL}")
print(f"      FOLLOW_WALLETS={len(FOLLOW_WALLETS)} wallets")
print(f"      RPC_URLS={len(RPC_URLS)} endpoints")
print(f"      SLIPPAGE={SLIPPAGE_TOLERANCE*100:.0f}%")
print(f"      POLLING={POLLING_INTERVAL_SEC}s")
print(f"      AUTO_DISCOVER={AUTO_DISCOVER}")
print(f"      STOP_LOSS={STOP_LOSS_PCT*100:.0f}% TP={TAKE_PROFIT_PCT*100:.0f}%")
check(USER_CAPITAL > 0, f"USER_CAPITAL={USER_CAPITAL}")
check(len(RPC_URLS) >= 1, "RPC_URLS empty")
ok()

# ===== 2. CLOB Auth =====
print("\n--- [2] CLOB Authentication ---")
from trader import get_client, get_deposit_wallet

t("CLOB client init")
client = get_client()
eoa = client.get_address()
print(f"      EOA: {eoa}")
check(eoa.startswith("0x"), f"Invalid EOA: {eoa}")
ok()

t("deposit wallet")
dw = get_deposit_wallet()
print(f"      Deposit: {dw}")
check(dw and dw.startswith("0x"), f"Invalid: {dw}")
ok()

# ===== 3. Balance =====
print("\n--- [3] Balance & Positions ---")
from trader import get_own_balance, get_pol_balance, get_own_positions

t("USDC balance")
b = get_own_balance()
print(f"      ${b:.2f}" if b else "      FAILED")
check(b is not None and b > 0, f"balance=${b}")
ok(f"${b:.2f}")

t("POL gas")
p = get_pol_balance()
print(f"      {p:.4f} POL" if p else "      FAILED")
check(p is not None and p > 0.01, f"POL={p}")
ok(f"{p:.4f} POL")

t("own positions")
pos = get_own_positions()
print(f"      {len(pos)} positions")
for pp in pos[:5]:
    print(f"        {pp.get('title','?')[:35]} val=${pp.get('current_value',0):.2f} pnl=${pp.get('cash_pnl',0):+.2f}")
ok(f"{len(pos)} pos")

# ===== 4. Order Book =====
print("\n--- [4] Order Book & Liquidity ---")
from trader import get_order_book, get_liquidity_depth
TOKEN = "21742633143463906290569050155826241533067272736897614950488156847949938836455"

t("order book")
book = get_order_book(TOKEN)
if book:
    print(f"      bids={len(book.get('bids',[]))} asks={len(book.get('asks',[]))}")
else:
    print(f"      None (market closed)")
check(book is None or isinstance(book, dict), f"type={type(book)}")
ok()

t("liquidity depth")
d = get_liquidity_depth(TOKEN, "BUY", 10)
print(f"      {d:.1f} shares" if d > 0 else f"      {d}")
ok()

# ===== 5. Leaderboard =====
print("\n--- [5] Leaderboard & Trader Data ---")
from leaderboard import (get_leaderboard, get_trader_value, get_trader_positions,
                          get_trader_trades, get_all_leaderboards, scan_newbies)

t("leaderboard top 5")
lb = get_leaderboard(limit=5)
for tr in lb[:3]:
    print(f"        #{tr['rank']} {tr['username'][:15]} PnL=${tr['pnl']:,.0f}")
check(len(lb) > 0, "empty")
ok(f"{len(lb)} traders")

t("trader value (primary)")
tv = get_trader_value(FOLLOW_WALLETS[0])
print(f"      ${tv:,.0f}")
check(tv > 0, f"${tv}")
ok(f"${tv:,.0f}")

t("trader positions")
tpos = get_trader_positions(FOLLOW_WALLETS[0])
print(f"      {len(tpos)} positions")
for tp in tpos[:3]:
    print(f"        {tp.get('title','?')[:30]} val=${tp.get('current_value',0):.2f}")
ok(f"{len(tpos)} pos")

t("trader trades (paginated)")
trades = get_trader_trades(FOLLOW_WALLETS[0], limit=50)
print(f"      {len(trades)} recent")
if trades:
    t0 = trades[0]
    usd = float(t0.get('size',0)) * float(t0.get('price',0))
    print(f"        Latest: {t0.get('side')} ${usd:.1f}")
ok(f"{len(trades)} trades")

t("get_all_leaderboards")
all_t = get_all_leaderboards(top_n=10)
print(f"      {len(all_t)} unique traders")
check(len(all_t) > 0, "empty")
ok(f"{len(all_t)} traders")

t("scan_newbies")
nb = scan_newbies()
print(f"      {len(nb)} candidates")
ok(f"{len(nb)} newbies")

# ===== 6. Score Engine =====
print("\n--- [6] Score Engine & Cascade Allocation ---")
from trader_score_engine import TraderScoreEngine

se = TraderScoreEngine()

t("score engine state")
print(f"      snapshots={len(se._snapshots)} blocks={len(se._emergency_blocks)} allocs={len(se._allocation_state)}")
ok()

t("score computation")
se.update_snapshot("0xTEST1", portfolio_value=50000, rank=25, pnl=5000)
s = se.get_score("0xTEST1")
print(f"      score={s['score']} mult={s['multiplier']} days={s['days']}")
check(0 <= s["score"] <= 100, f"score={s['score']}")
ok(f"score={s['score']}")

t("cascade allocation")
se.update_snapshot("0xTEST1", portfolio_value=50000, rank=10, pnl=10000)
se.update_snapshot("0xTEST2", portfolio_value=30000, rank=50, pnl=3000)
a1 = se.compute_allocation("0xTEST1", 100, ["0xTEST1", "0xTEST2"])
a2 = se.compute_allocation("0xTEST2", 100, ["0xTEST1", "0xTEST2"])
print(f"      w1=${a1:.1f} w2=${a2:.1f} (total $100)")
check(a1 + a2 <= 100.1, f"sum={a1+a2}")
ok()

t("allocation track/release")
se.record_allocation_used("0xTEST1", 20)
se.release_allocation("0xTEST1", 15)
st = se._allocation_state.get("0xTEST1", {})
print(f"      used=${st.get('used',0):.1f} cap=${st.get('cap',0):.1f}")
ok()

t("emergency block")
se.set_emergency_block("0xTEST3", "Test block reason")
check(se.is_blocked("0xTEST3"), "should be blocked")
s3 = se.get_score("0xTEST3")
check(s3["multiplier"] == 0.0, f"mult={s3['multiplier']}")
print(f"      mult=0.0 (blocked)")
se.clear_emergency_block("0xTEST3")
check(not se.is_blocked("0xTEST3"), "should be unblocked")
ok()

# ===== 7. Trade Functions =====
print("\n--- [7] Trade Execution (dry validation) ---")
from trader import (calculate_copy_size, place_copy_order, poll_all_trader_buys,
                     poll_all_trader_sells)

t("calculate_copy_size")
sz = calculate_copy_size(5000, FOLLOW_WALLETS[0])
print(f"      ${sz:.2f} for $5K trade")
check(sz > 0, f"sz={sz}")
ok(f"${sz:.2f}")

t("invalid order rejection")
ok2, oid2, err = place_copy_order("bad_token", "BUY", 0.5, 100)
check(not ok2, "should reject bad token")
print(f"      Rejected: {str(err)[:60]}")
ok()

t("poll buys")
buys = poll_all_trader_buys(FOLLOW_WALLETS[:1])
total_b = sum(len(v) for v in buys.values())
print(f"      {total_b} signals")
ok(f"{total_b} signals")

t("poll sells")
sells = poll_all_trader_sells(FOLLOW_WALLETS[:1])
total_s = sum(len(v) for v in sells.values())
print(f"      {total_s} signals")
ok(f"{total_s} signals")

# ===== 8. Data API =====
print("\n--- [8] Data API Endpoints ---")
from config import safe_get, DATA_API, GAMMA_API

t("safe_get leaderboard")
r = safe_get(f"{DATA_API}/v1/leaderboard",
    params={"limit":3,"category":"OVERALL","timePeriod":"MONTH","orderBy":"PNL"}, timeout=10)
check(r is not None and r.status_code == 200, f"s={r.status_code if r else 'None'}")
print(f"      200 OK, {len(r.json())} entries")
ok()

t("safe_get gamma/markets")
r = safe_get(f"{GAMMA_API}/markets", params={"limit":3}, timeout=10)
check(r is not None and r.status_code == 200, f"s={r.status_code if r else 'None'}")
print(f"      200 OK")
ok()

t("safe_get 404 tolerance")
r = safe_get(f"{DATA_API}/trades",
    params={"user":"0x0000000000000000000000000000000000000000"}, timeout=10)
check(r is not None, "None")
print(f"      status={r.status_code}")
ok()

# ===== 9. Feishu =====
print("\n--- [9] Feishu Notification ---")
from common.feishu import send_feishu

t("feishu dry send")
result = send_feishu(FEISHU_WEBHOOK, "Bot自检", ["**状态**: 正常", "**时间**: 测试"], color="blue")
print(f"      {'Sent' if result else 'Skipped'}")
ok()

# ===== 10. Edge Cases =====
print("\n--- [10] Edge Cases ---")
import requests as req

t("empty wallets poll")
r_empty = poll_all_trader_buys([])
check(r_empty == {}, f"{r_empty}")
print(f"      empty in → empty out")
ok()

t("RPC endpoint")
rpc = req.post(RPC_URLS[0],
    json={"jsonrpc":"2.0","method":"eth_blockNumber","params":[],"id":1}, timeout=10)
check(rpc.status_code == 200, f"RPC s={rpc.status_code}")
blk = int(rpc.json()["result"], 16)
print(f"      Block: {blk}")
ok(f"block {blk}")

t("garbage token order book")
bk = get_order_book("deadbeef")
print(f"      {'None (expected)' if bk is None else f'Got book with {len(bk)} keys'}")
check(bk is None or isinstance(bk, dict), f"type={type(bk)}")
ok()

# ===== Summary =====
print()
print("=" * 60)
passed = sum(1 for r in RESULTS if r["status"] == "PASS")
failed = sum(1 for r in RESULTS if r["status"] == "FAIL")
total = len(RESULTS)
rate = passed/total*100 if total else 0
print(f"  RESULTS: {passed} PASS, {failed} FAIL, {total} TOTAL ({rate:.0f}%)")
if failed:
    print("  FAILURES:")
    for r in RESULTS:
        if r["status"] == "FAIL":
            print(f"    - {r['name']}: {r['error'][:150]}")
print("=" * 60)

# Save report
out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "results", "test_report.json")
os.makedirs(os.path.dirname(out), exist_ok=True)
with open(out, "w") as f:
    json.dump({
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()),
        "passed": passed, "failed": failed, "total": total, "rate": round(rate, 1),
        "results": RESULTS,
    }, f, indent=2, ensure_ascii=False)
print(f"\nReport saved to results/test_report.json")
