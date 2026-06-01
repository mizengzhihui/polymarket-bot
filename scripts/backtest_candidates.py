"""
Backtest candidates using the real bot's calculate_copy_size + get_trader_value.
Generates backtest_*.json files compatible with the VPS format.
"""
import json, os, sys, time, requests
from datetime import datetime, timezone
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import (
    MIN_TRADE_SIZE, MAX_POSITION_PCT, MAX_POSITION_SIZE, MAX_TOTAL_EXPOSURE,
    MAX_DAILY_LOSS, MAX_PORTFOLIO_LOSS, USER_CAPITAL, get_wallet_config,
)
from leaderboard import get_trader_value

DATA_API = "https://data-api.polymarket.com"
RESULT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "results")


def fetch_trades(wallet, limit=500):
    """Fetch trades for a wallet."""
    url = f"{DATA_API}/trades"
    params = {"user": wallet, "limit": limit, "takerOnly": True}
    try:
        resp = requests.get(url, params=params, timeout=20)
        resp.raise_for_status()
        trades = []
        for t in resp.json():
            ts = t.get("timestamp", 0)
            try:
                ts_int = int(ts)
                if ts_int > 1000000000000:
                    ts_int //= 1000
            except (ValueError, TypeError):
                ts_int = 0
            trades.append({
                "timestamp": ts_int,
                "side": t.get("side", ""),
                "size": float(t.get("size", 0)),
                "price": float(t.get("price", 0)),
                "asset": t.get("asset", ""),
                "condition_id": t.get("conditionId", ""),
                "title": t.get("title", ""),
                "tx_hash": t.get("transactionHash", ""),
            })
        trades.sort(key=lambda x: x["timestamp"])
        return trades
    except Exception as e:
        print(f"  ERROR fetching trades: {e}")
        return []


def backtest_wallet(wallet, label, capital):
    """Run backtest using the ACTUAL bot's copy logic via calculate_copy_size."""
    from trader import calculate_copy_size

    trades = fetch_trades(wallet, limit=500)
    if not trades:
        return None

    usdc = float(capital)
    initial = usdc
    positions = {}  # asset -> {shares, avg_price, current_price, title}
    trader_net = defaultdict(float)  # asset -> trader's cumulative net shares
    closed = []
    daily_realized = defaultdict(float)
    daily_trades = defaultdict(int)
    daily_loss = 0.0
    last_day = None
    executed = 0
    skipped = defaultdict(int)

    # Trader portfolio tries: cached value first, then API
    trader_value = get_trader_value(wallet)
    if trader_value <= 0:
        trader_value = 10000  # fallback

    for trade in trades:
        ts = int(trade["timestamp"])
        day = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")

        if day != last_day:
            daily_loss = 0.0
            last_day = day

        if daily_loss >= MAX_DAILY_LOSS:
            skipped["daily_loss"] += 1
            continue

        size = float(trade["size"])
        price = float(trade["price"])
        asset = trade["asset"]
        title = trade.get("title", "")
        side = trade["side"]

        if side == "BUY":
            trader_net[asset] += size
            trade_value = size * price
            if trade_value < MIN_TRADE_SIZE:
                skipped["too_small"] += 1
                continue

            # Use actual bot's calculate_copy_size
            copy_usd = calculate_copy_size(trade_value, wallet)
            if copy_usd <= 0:
                skipped["zero_copy"] += 1
                continue

            copy_usd = min(copy_usd, MAX_POSITION_SIZE)

            # Exposure cap
            total_open = sum(p["shares"] * p.get("price", p["avg"]) for p in positions.values())
            remaining = (usdc * MAX_TOTAL_EXPOSURE) - total_open
            if copy_usd > remaining and remaining > 0:
                copy_usd = remaining
            if remaining <= 0:
                skipped["exposure"] += 1
                continue

            # Portfolio drawdown cap
            unrealized = sum(p["shares"] * (p.get("price", p["avg"]) - p["avg"]) for p in positions.values())
            if unrealized < -MAX_PORTFOLIO_LOSS:
                copy_usd *= 0.25
                if copy_usd < MIN_TRADE_SIZE:
                    skipped["portfolio_loss"] += 1
                    continue

            cost = copy_usd
            if cost > usdc * 0.95:
                skipped["capital"] += 1
                continue

            shares = cost / price if price > 0 else 0
            if shares <= 0:
                continue

            usdc -= cost
            if asset in positions:
                p = positions[asset]
                p["avg"] = (p["shares"] * p["avg"] + cost) / (p["shares"] + shares)
                p["shares"] += shares
                p["price"] = price
            else:
                positions[asset] = {"shares": shares, "avg": price, "price": price, "title": title}
            executed += 1
            daily_trades[day] += 1

        elif side == "SELL":
            if asset not in positions:
                trader_net[asset] = max(0, trader_net.get(asset, 0) - size)
                skipped["no_position"] += 1
                continue

            p = positions[asset]
            tn = trader_net.get(asset, 0)
            ratio = min(size / tn, 1.0) if tn > 0 else 1.0
            sell_shares = round(p["shares"] * ratio, 2)
            sell_shares = min(sell_shares, p["shares"])

            if sell_shares < 0.01:
                trader_net[asset] = max(0, tn - size)
                skipped["tiny_sell"] += 1
                continue

            revenue = sell_shares * price
            pnl = revenue - sell_shares * p["avg"]

            usdc += revenue
            p["shares"] -= sell_shares
            p["price"] = price
            trader_net[asset] = max(0, tn - size)

            closed.append({
                "asset": asset, "title": title, "shares": sell_shares,
                "entry": p["avg"], "exit": price, "pnl": round(pnl, 2), "date": day,
            })

            daily_realized[day] += pnl
            if pnl < 0:
                daily_loss += abs(pnl)

            if p["shares"] < 0.01:
                del positions[asset]
                trader_net.pop(asset, None)
            executed += 1
            daily_trades[day] += 1

    # Close remaining positions at last price
    final_unrealized = 0
    for asset, p in positions.items():
        price = p.get("price", p["avg"])
        val = p["shares"] * price
        pnl = val - p["shares"] * p["avg"]
        usdc += val
        final_unrealized += pnl
        closed.append({
            "asset": asset, "title": p.get("title", ""), "shares": p["shares"],
            "entry": p["avg"], "exit": price, "pnl": round(pnl, 2), "date": "FINAL",
        })

    # Stats
    total_pnl = usdc - initial
    ret_pct = (total_pnl / initial) * 100

    wins = sum(1 for c in closed if c["pnl"] > 0)
    losses = sum(1 for c in closed if c["pnl"] < 0)
    total_c = wins + losses

    gross_win = sum(c["pnl"] for c in closed if c["pnl"] > 0)
    gross_loss = abs(sum(c["pnl"] for c in closed if c["pnl"] < 0))
    pf = gross_win / gross_loss if gross_loss > 0 else 999

    # Max drawdown from daily equity
    peak = initial
    max_dd = 0.0
    equity = [(list(daily_realized.keys())[0] if daily_realized else "start", initial)]
    cum = initial
    for day, pnl in sorted(daily_realized.items()):
        cum += pnl
        equity.append((day, round(cum, 2)))
        peak = max(peak, cum)
        if peak > 0:
            max_dd = max(max_dd, (peak - cum) / peak * 100)

    winning_days = sum(1 for v in daily_realized.values() if v > 0)
    losing_days = sum(1 for v in daily_realized.values() if v < 0)

    result = {
        "wallet": wallet,
        "label": label,
        "period": f"{datetime.fromtimestamp(trades[0]['timestamp'], tz=timezone.utc).strftime('%Y-%m-%d')} to {datetime.fromtimestamp(trades[-1]['timestamp'], tz=timezone.utc).strftime('%Y-%m-%d')} ({len(trades)} trades)",
        "trader_portfolio": round(trader_value, 2),
        "total_trades": len(trades),
        "copied_trades": executed,
        "skipped_no_capital": skipped.get("capital", 0),
        "skipped_exposure": skipped.get("exposure", 0),
        "skipped_total": sum(skipped.values()),
        "daily_pnl": dict(sorted(daily_realized.items())),
        "max_drawdown": round(max_dd, 2),
        "peak_capital": round(peak, 2),
        "final_capital": round(usdc, 2),
        "total_pnl": round(total_pnl, 2),
        "return_pct": round(ret_pct, 2),
        "profit_factor": round(pf, 2),
        "remaining_positions": len(positions),
        "winning_days": winning_days,
        "losing_days": losing_days,
        "total_days": len(daily_trades),
        "closed_trades": total_c,
        "wins": wins,
        "losses": losses,
        "win_rate": round(wins / total_c * 100, 1) if total_c > 0 else 0,
    }
    return result


def main():
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--capital", type=int, default=1000)
    p.add_argument("--wallets", type=str, default="",
                   help="Comma-separated wallet addresses (default: from wallet_selection.json)")
    p.add_argument("--limit", type=int, default=5, help="Max wallets to test")
    args = p.parse_args()

    CAP = args.capital

    # Load candidates
    wallets_to_test = []
    if args.wallets:
        wallets_to_test = [w.strip() for w in args.wallets.split(",") if w.strip()]
    else:
        sel_path = os.path.join(RESULT_DIR, "wallet_selection.json")
        if os.path.exists(sel_path):
            with open(sel_path) as f:
                sel = json.load(f)
            wallets_to_test = [c["wallet"] for c in sel["selected"][:args.limit]]
            # Also add VPenguin for comparison
            vpenguin = "0xfbf3d501e88815464642d0e913f15379c3eeb218"
            if vpenguin not in wallets_to_test:
                wallets_to_test.append(vpenguin)
        else:
            print("No wallet_selection.json found. Use --wallets to specify.")
            return

    print(f"\n{'='*60}")
    print(f"  Poly_Copy — Candidate Backtest (using live bot logic)")
    print(f"  Capital: ${CAP}  |  Wallets: {len(wallets_to_test)}")
    print(f"{'='*60}")

    results = []

    for i, wallet in enumerate(wallets_to_test, 1):
        label = f"candidate_{i}" if not wallet.startswith("0xfbf3") else "VPenguin"
        short = wallet[:10]
        print(f"\n  [{i}/{len(wallets_to_test)}] {short}... (fetching trades)", end=" ", flush=True)

        result = backtest_wallet(wallet, label, CAP)
        if result is None:
            print("SKIP (no data)")
            continue

        print(f"\n    Period: {result['period'][:50]}")
        print(f"    Copied: {result['copied_trades']}/{result['total_trades']} trades  |  Skipped: {result['skipped_total']}")
        print(f"    Return: {result['return_pct']:+.1f}%  |  Max DD: {result['max_drawdown']:.1f}%  |  PF: {result['profit_factor']}")
        print(f"    Win days: {result['winning_days']}/{result['total_days']}  |  Win rate: {result['win_rate']}%")

        results.append(result)

        # Save individual result
        out_path = os.path.join(RESULT_DIR, f"backtest_{wallet[:10].replace('0x','')}.json")
        with open(out_path, "w") as f:
            json.dump(result, f, indent=2)

        time.sleep(0.3)

    # Summary
    if results:
        summary = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "capital": CAP,
            "copy_multiplier": "live (calculate_copy_size)",
            "results": results,
        }
        summary_path = os.path.join(RESULT_DIR, "backtest_candidates.json")
        with open(summary_path, "w") as f:
            json.dump(summary, f, indent=2, ensure_ascii=False)

        print(f"\n{'─'*60}")
        print(f"  📊 Summary ({len(results)} wallets)")
        print(f"{'─'*60}")
        for r in results:
            status = "✅" if r["return_pct"] > 0 and r["max_drawdown"] < 50 else ("⚠️" if r["return_pct"] > 0 else "❌")
            print(f"  {status} {r['label']:15s} {r['return_pct']:>+8.1f}%  DD={r['max_drawdown']:.0f}%  PF={r['profit_factor']}  WinDays={r['winning_days']}/{r['total_days']}")
        print(f"\n  Results saved: {summary_path}")

    print(f"{'='*60}")


if __name__ == "__main__":
    main()
