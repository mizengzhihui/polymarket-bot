"""
Multi-Trader Copy-Trading Backtest — Enhanced Edition
VPenguin + ChloeT1, full scenario analysis with stress testing.
"""
import json, os, sys, math, random
from collections import defaultdict
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import MIN_TRADE_SIZE, MAX_POSITION_PCT, MAX_TOTAL_EXPOSURE, get_wallet_config

COPY_MULTIPLIER = 0.5
MAX_DAILY_LOSS = 5.0
MAX_PORTFOLIO_LOSS = 50.0
MAX_POSITION_LOSS = 2.35
RESULT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results", "backtest")

TRADERS = {
    "VPenguin": {
        "wallet": "0xfbf3d501e88815464642d0e913f15379c3eeb218",
        "portfolio_estimate": 65_000,
    },
    "ChloeT1": {
        "wallet": "0x9ac2536ed93f8fe8ce91d9662b03bcbb19ccbe3d",
        "portfolio_estimate": 750_000,
    },
}


def fetch_trades(wallet):
    import requests
    resp = requests.get(
        'https://data-api.polymarket.com/trades',
        params={'user': wallet, 'limit': 1000, 'takerOnly': True}, timeout=30)
    data = resp.json()
    # Normalize timestamps: ensure seconds, not milliseconds
    for t in data:
        raw = t.get('timestamp', 0)
        try:
            ts = int(raw)
            if ts > 1000000000000:  # Likely milliseconds (13+ digits)
                ts = ts // 1000
            t['timestamp'] = ts
        except (ValueError, TypeError):
            t['timestamp'] = 0
    data.sort(key=lambda x: int(x.get('timestamp', 0)))
    return data


def load_or_fetch(name, wallet, refresh=False):
    path = os.path.join(RESULT_DIR, f"trades_{name}.json")
    if not refresh and os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    os.makedirs(RESULT_DIR, exist_ok=True)
    trades = fetch_trades(wallet)
    with open(path, 'w') as f:
        json.dump(trades, f)
    return trades


class BacktestEngine:
    def __init__(self, trader_name, capital, trades, trader_portfolio, wallet=""):
        self.name = trader_name
        self.capital = capital
        self.initial = capital
        self.trades = trades
        self.trader_portfolio = trader_portfolio
        self.wallet = wallet

        self.usdc = float(capital)
        self.positions = {}
        self.closed = []
        self.trader_net = defaultdict(float)

        self.daily_realized = defaultdict(float)
        self.daily_trades = defaultdict(int)
        self.daily_loss = 0.0
        self.last_day = None

        self.executed = 0
        self.buys = 0
        self.sells = 0
        self.skipped = defaultdict(int)

        self.cb = False
        self.cb_date = None
        self.equity = []

        # Per-wallet overrides
        wcfg = get_wallet_config(wallet) if wallet else {}
        self.copy_mult = wcfg.get("copy_multiplier", COPY_MULTIPLIER)
        self.pos_pct = wcfg.get("max_position_pct", MAX_POSITION_PCT)
        self.pos_loss = wcfg.get("max_position_loss", MAX_POSITION_LOSS)

    def day(self, ts):
        return datetime.fromtimestamp(ts, tz=timezone.utc).strftime('%Y-%m-%d')

    def reset_day(self, ts):
        d = self.day(ts)
        if d != self.last_day:
            if self.last_day:
                eq = self.usdc + self.unrealized_pnl()
                self.equity.append((self.last_day, round(eq, 2)))
            self.daily_loss = 0.0
            self.last_day = d

    def cb_active(self, ts):
        d = self.day(ts)
        if self.cb and self.cb_date == d:
            return True
        self.cb = False
        return False

    def exposure(self):
        v = sum(p['shares'] * p.get('price', p['avg']) for p in self.positions.values())
        return v / self.capital if self.capital > 0 else 0

    def unrealized_pnl(self):
        return sum(p['shares'] * (p.get('price', p['avg']) - p['avg'])
                   for p in self.positions.values())

    def buy(self, trade):
        size = float(trade['size'])
        price = float(trade['price'])
        asset = trade['asset']
        title = trade.get('title', '')
        ts = int(trade['timestamp'])

        self.trader_net[asset] += size

        ratio = size / self.trader_portfolio
        copy = self.capital * ratio * self.copy_mult
        copy = min(copy, self.capital * self.pos_pct)
        copy = max(MIN_TRADE_SIZE, round(copy, 2))
        cost = copy * price

        if self.cb_active(ts): self.skipped['cb'] += 1; return
        if self.daily_loss >= MAX_DAILY_LOSS: self.skipped['daily_loss'] += 1; return
        if cost > self.usdc * 0.95: self.skipped['capital'] += 1; return

        exp = self.exposure()
        if exp + cost / self.capital > MAX_TOTAL_EXPOSURE:
            avail = (MAX_TOTAL_EXPOSURE - exp) * self.capital
            if avail < MIN_TRADE_SIZE * price: self.skipped['exposure'] += 1; return
            copy = min(copy, avail / price)
            cost = copy * price

        if self.unrealized_pnl() < -MAX_PORTFOLIO_LOSS:
            copy *= 0.25; cost = copy * price
            if copy < MIN_TRADE_SIZE: self.skipped['portfolio_loss'] += 1; return

        if copy < MIN_TRADE_SIZE: self.skipped['too_small'] += 1; return

        self.usdc -= cost
        if asset in self.positions:
            p = self.positions[asset]
            p['avg'] = (p['shares'] * p['avg'] + cost) / (p['shares'] + copy)
            p['shares'] += copy
            p['price'] = price
        else:
            self.positions[asset] = {'shares': copy, 'avg': price, 'price': price, 'title': title}
        self.executed += 1; self.buys += 1
        self.daily_trades[self.day(ts)] += 1

    def sell(self, trade):
        size = float(trade['size'])
        price = float(trade['price'])
        asset = trade['asset']
        ts = int(trade['timestamp'])

        if asset not in self.positions:
            self.skipped['no_position'] += 1
            self.trader_net[asset] = max(0, self.trader_net.get(asset, 0) - size)
            return

        p = self.positions[asset]
        tn = self.trader_net.get(asset, 0)
        ratio = min(size / tn, 1.0) if tn > 0 else 1.0
        shares = round(p['shares'] * ratio, 2)
        shares = min(shares, p['shares'])

        if shares < 0.01:
            self.skipped['tiny_sell'] += 1
            self.trader_net[asset] = max(0, tn - size)
            return

        revenue = shares * price
        pnl = revenue - shares * p['avg']

        self.usdc += revenue
        p['shares'] -= shares
        p['price'] = price
        self.trader_net[asset] = max(0, tn - size)

        self.closed.append({'asset': asset, 'title': p.get('title', ''),
            'shares': shares, 'entry': p['avg'], 'exit': price, 'pnl': round(pnl, 2),
            'date': self.day(ts)})

        d = self.day(ts)
        self.daily_realized[d] += pnl
        self.daily_loss += max(0, -pnl)

        if p['shares'] < 0.01:
            del self.positions[asset]
            self.trader_net.pop(asset, None)

        self.executed += 1; self.sells += 1
        self.daily_trades[d] += 1

        # Stop-loss trigger (per-position)
        if pnl < -self.pos_loss:
            self.skipped['stop_loss'] += 1

        if self.daily_loss >= MAX_DAILY_LOSS:
            self.cb = True
            self.cb_date = d

    def run(self):
        for t in self.trades:
            self.reset_day(int(t['timestamp']))
            if t['side'] == 'BUY':
                self.buy(t)
            else:
                self.sell(t)

        if self.last_day:
            self.equity.append((self.last_day, round(self.usdc + self.unrealized_pnl(), 2)))

        for asset, p in list(self.positions.items()):
            price = p.get('price', p['avg'])
            val = p['shares'] * price
            pnl = val - p['shares'] * p['avg']
            self.usdc += val
            self.closed.append({'asset': asset, 'title': p.get('title', ''),
                'shares': p['shares'], 'entry': p['avg'], 'exit': price,
                'pnl': round(pnl, 2), 'date': 'FINAL'})
        self.positions = {}

    def stats(self):
        realized = sum(c['pnl'] for c in self.closed)
        final = self.usdc
        ret = (final - self.initial) / self.initial * 100

        wins = sum(1 for c in self.closed if c['pnl'] > 0)
        losses = sum(1 for c in self.closed if c['pnl'] < 0)
        total_c = wins + losses

        if self.trades:
            days = (int(self.trades[-1]['timestamp']) - int(self.trades[0]['timestamp'])) / 86400
            ann = ((1 + ret/100) ** (365/days) - 1) * 100 if days > 0 and ret > -100 else 0
        else:
            days = 0; ann = 0

        best = max(self.daily_realized.items(), key=lambda x: x[1]) if self.daily_realized else ('-', 0)
        worst = min(self.daily_realized.items(), key=lambda x: x[1]) if self.daily_realized else ('-', 0)

        peak = 0; max_dd = 0.0
        for _, eq in self.equity:
            peak = max(peak, eq)
            if peak > 0:
                max_dd = max(max_dd, (peak - eq) / peak * 100)

        gross_win = sum(c['pnl'] for c in self.closed if c['pnl'] > 0)
        gross_loss = abs(sum(c['pnl'] for c in self.closed if c['pnl'] < 0))
        pf = gross_win / gross_loss if gross_loss > 0 else float('inf')

        # Per-position PnL distribution
        pnl_values = [c['pnl'] for c in self.closed]
        avg_win = sum(c['pnl'] for c in self.closed if c['pnl'] > 0) / max(wins, 1)
        avg_loss = sum(c['pnl'] for c in self.closed if c['pnl'] < 0) / max(losses, 1)

        return {
            'trader': self.name,
            'period_days': round(days),
            'trading_days': len(self.daily_trades),
            'initial': self.initial, 'final': round(final, 2),
            'pnl': round(final - self.initial, 2),
            'return_pct': round(ret, 2),
            'annualized_pct': round(ann, 1),
            'max_dd_pct': round(max_dd, 1),
            'profit_factor': round(pf, 2) if pf != float('inf') else 999,
            'executed': self.executed, 'buys': self.buys, 'sells': self.sells,
            'skipped': dict(self.skipped), 'total_skipped': sum(self.skipped.values()),
            'closed': total_c, 'wins': wins, 'losses': losses,
            'win_rate': round(wins / total_c * 100, 1) if total_c > 0 else 0,
            'realized': round(realized, 2),
            'avg_win': round(avg_win, 2), 'avg_loss': round(avg_loss, 2),
            'best_day': f"${best[1]:,.2f} ({best[0]})",
            'worst_day': f"${worst[1]:,.2f} ({worst[0]})",
            'equity': self.equity,
            'daily_returns': list(self.daily_realized.values()),
        }


def monte_carlo_trade_sim(rtrades, capital, target_days=365, sims=5000):
    """Trade-level Monte Carlo: simulate daily trade count + per-trade PnL.

    Much more realistic than daily-return bootstrap when we have few trading days
    but many individual trades with known PnL distributions.
    """
    if not rtrades or len(rtrades) < 5:
        return None

    pnl_values = [t['pnl'] for t in rtrades]
    wins = [v for v in pnl_values if v > 0]
    losses = [v for v in pnl_values if v < 0]
    win_rate = len(wins) / len(pnl_values)

    if not wins or not losses:
        # Degenerate: all wins or all losses — use conservative defaults
        avg_win = sum(wins) / max(len(wins), 1) if wins else 1.0
        avg_loss = sum(losses) / max(len(losses), 1) if losses else -0.5
    else:
        avg_win = sum(wins) / len(wins)
        avg_loss = sum(losses) / len(losses)

    # Average trades per day from the data
    trades_per_day = max(1, len(pnl_values) / max(target_days * 0.5, 1))

    finals = []
    paths_min = []  # track minimum portfolio along each path

    for _ in range(sims):
        val = float(capital)
        min_val = val
        for day in range(target_days):
            # Daily trade count (Poisson-like via binomial approximation)
            n_trades = max(0, int(random.gauss(trades_per_day, trades_per_day * 0.5)))
            for _ in range(n_trades):
                if random.random() < win_rate:
                    trade_pnl = random.choice(wins) if wins else avg_win
                else:
                    trade_pnl = random.choice(losses) if losses else avg_loss
                # Apply position cap: max trade PnL is bounded by position size
                trade_pnl = max(trade_pnl, -capital * MAX_POSITION_PCT)
                val += trade_pnl
                if val <= 0:
                    val = 0
                    break
            min_val = min(min_val, val)
            if val <= 0:
                break

        finals.append(val)
        paths_min.append(min_val)

    finals.sort()
    n_sims = len(finals)

    def pctile(p):
        idx = max(0, min(n_sims - 1, int(n_sims * p / 100)))
        return finals[idx]

    ruin_50 = sum(1 for v in finals if v <= capital * 0.5) / n_sims * 100
    ruin_10 = sum(1 for v in finals if v <= capital * 0.1) / n_sims * 100
    bankrupt = sum(1 for v in finals if v <= 0) / n_sims * 100

    avg_dd = sum(1 - (m / max(capital, 1)) for m in paths_min) / n_sims * 100

    return {
        'median_final': round(finals[n_sims // 2], 2),
        'median_return_pct': round((finals[n_sims // 2] - capital) / capital * 100, 1),
        'mean_return_pct': round((sum(finals)/n_sims - capital) / capital * 100, 1),
        'p95_final': round(pctile(95), 2),
        'p05_final': round(pctile(5), 2),
        'p01_final': round(pctile(1), 2),
        'ruin_50pct_pct': round(ruin_50, 2),
        'ruin_10pct_pct': round(ruin_10, 2),
        'bankrupt_pct': round(bankrupt, 2),
        'avg_drawdown_pct': round(avg_dd, 1),
        'scenarios': {
            'best': round((pctile(95) - capital) / capital * 100, 1),
            'base': round((finals[n_sims // 2] - capital) / capital * 100, 1),
            'bear': round((pctile(25) - capital) / capital * 100, 1),
            'worst': round((pctile(5) - capital) / capital * 100, 1),
            'disaster': round((pctile(1) - capital) / capital * 100, 1),
        }
    }


STRESS_SCENARIOS = {
    "Base (historical replay)": {"chloe_loss_mult": 1.0, "win_drop": 0.0},
    "Mild headwind (-20% win rate)": {"chloe_loss_mult": 1.0, "win_drop": 0.20},
    "Severe headwind (-40% win rate)": {"chloe_loss_mult": 1.5, "win_drop": 0.40},
    "Worst case (double event + crash)": {"chloe_loss_mult": 2.0, "win_drop": 0.50},
}


def run_stress_scenario(name, params, base_trades_by_trader, capital):
    """Run stress-modified trade-level Monte Carlo for combined portfolio."""
    all_rtrades = []
    for tname, rtrades in base_trades_by_trader.items():
        modified = []
        for t in rtrades:
            pnl = t['pnl']
            # Reduce win rate: randomly flip some wins to losses
            if tname == "ChloeT1" and pnl > 0 and random.random() < params["chloe_loss_mult"] * 0.03:
                pnl = pnl * -1.5  # ChloeT1 event: a win turns into a bigger loss
            elif pnl > 0 and random.random() < params["win_drop"]:
                pnl = -abs(pnl)  # Win becomes loss
            modified.append({**t, 'pnl': pnl})
        all_rtrades.extend(modified)

    total_capital = capital * 2
    return monte_carlo_trade_sim(all_rtrades, total_capital, target_days=365, sims=3000)


def print_bar(label, value, max_val, width=40):
    """Print a horizontal bar chart."""
    bar_len = int(abs(value) / max(max_val, 1) * width)
    bar = '█' * min(bar_len, width)
    sign = '+' if value >= 0 else ''
    return f"  {label:<20} {sign}{value:>6.1f}%  {bar}"


def main():
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument('--capital', type=int, default=500)
    p.add_argument('--refresh', action='store_true')
    p.add_argument('--mc', type=int, default=365, help='Monte Carlo target days (default: 365)')
    args = p.parse_args()

    CAP = args.capital
    print("=" * 70)
    print(f"  Poly_Copy Full Backtest -- VPenguin + ChloeT1")
    print(f"  Per-trader capital: ${CAP}  |  Combined: ${CAP*2}")
    print(f"  VPenguin: {COPY_MULTIPLIER}x, {MAX_POSITION_PCT*100:.0f}% cap, ${MAX_POSITION_LOSS:.2f} stop")
    print(f"  ChloeT1: 0.3x, 5% cap, $1.50 stop (per-wallet overrides)")
    print("=" * 70)

    # ========================================
    # Phase 1: Historical Backtest
    # ========================================
    print(f"\n{'─'*70}")
    print(f"  [Phase 1] Historical Backtest -- Replay Actual Trades")
    print(f"{'─'*70}")

    all_stats = {}
    all_rtrades = {}  # Per-trader realized trade PnL for MC
    all_pnls = {}

    for name, info in TRADERS.items():
        trades = load_or_fetch(name, info['wallet'], args.refresh)
        n_trades = len(trades)
        if n_trades >= 2:
            t0 = datetime.fromtimestamp(int(trades[0]['timestamp']), tz=timezone.utc)
            t1 = datetime.fromtimestamp(int(trades[-1]['timestamp']), tz=timezone.utc)
            span = (t1 - t0).days
            buys = sum(1 for t in trades if t['side'] == 'BUY')
            sells = sum(1 for t in trades if t['side'] == 'SELL')
            print(f"  [{name}] {n_trades} trades ({t0.strftime('%Y-%m-%d')} -> {t1.strftime('%Y-%m-%d')}, {span}d)")
            print(f"          BUY:{buys}  SELL:{sells}  ratio={buys/max(sells,1):.1f}:1")
        else:
            print(f"  [{name}] {n_trades} trades")

        engine = BacktestEngine(name, CAP, trades, info['portfolio_estimate'], info['wallet'])
        engine.run()
        r = engine.stats()
        all_stats[name] = r
        all_pnls[name] = r['pnl']
        all_rtrades[name] = engine.closed  # All realized trades with PnL

        print(f"\n  [{name}] Results (capital ${r['initial']:.0f}):")
        print(f"    Period: {r['period_days']}d  |  Trading days: {r['trading_days']}d  |  Executed: {r['executed']}")
        print(f"    Final: ${r['final']:,.2f}  |  PnL: ${r['pnl']:+,.2f}  ({r['return_pct']:+.1f}%)")
        print(f"    Annualized: {r['annualized_pct']:+.1f}%  |  Max DD: {r['max_dd_pct']:.1f}%  |  PF: {r['profit_factor']}")
        print(f"    Win rate: {r['win_rate']}%  |  Avg win: ${r['avg_win']:,.2f}  |  Avg loss: ${r['avg_loss']:,.2f}")
        print(f"    Skipped: {r['total_skipped']} ({', '.join(f'{k}={v}' for k,v in sorted(r['skipped'].items(), key=lambda x:-x[1])[:5])})")
        if r.get('best_day') and r.get('worst_day'):
            print(f"    Best day: {r['best_day']}  |  Worst day: {r['worst_day']}")
        # Per-trade PnL summary
        pnl_dist = [t['pnl'] for t in engine.closed]
        if pnl_dist:
            print(f"    Trade PnL range: ${min(pnl_dist):+,.2f} ~ ${max(pnl_dist):+,.2f}  |  Median: ${sorted(pnl_dist)[len(pnl_dist)//2]:+,.2f}")

    total_capital = CAP * len(TRADERS)
    combined_pnl = sum(all_pnls.values())
    combined_return = combined_pnl / total_capital * 100
    print(f"\n  >> Combined: Capital=${total_capital}  PnL=${combined_pnl:+,.2f}  Return={combined_return:+.1f}%")

    # ========================================
    # Phase 2: Trade-Level Monte Carlo -- Individual
    # ========================================
    print(f"\n{'─'*70}")
    print(f"  [Phase 2] Trade-Level Monte Carlo -- {args.mc}d horizon, 5000 sims")
    print(f"{'─'*70}")

    mc_results = {}
    for name, r in all_stats.items():
        rtrades = all_rtrades.get(name, [])
        if len(rtrades) >= 5:
            print(f"\n  [{name}] Raw trade PnL: {len(rtrades)} closed trades")
            mc = monte_carlo_trade_sim(rtrades, CAP, target_days=args.mc, sims=5000)
            mc_results[name] = mc
            if mc:
                print(f"    Median return: {mc['median_return_pct']:+.1f}%  |  Mean: {mc['mean_return_pct']:+.1f}%")
                print(f"    P95 (bull): ${mc['p95_final']:,.0f}  |  P05 (bear): ${mc['p05_final']:,.0f}")
                print(f"    P01 (disaster): ${mc['p01_final']:,.0f}  |  Bankrupt risk: {mc['bankrupt_pct']:.1f}%")
                print(f"    50% drawdown risk: {mc['ruin_50pct_pct']:.1f}%  |  90% drawdown risk: {mc['ruin_10pct_pct']:.1f}%")
                print(f"    Avg drawdown: {mc['avg_drawdown_pct']:.1f}%")

    # ========================================
    # Phase 3: Combined Portfolio Monte Carlo
    # ========================================
    print(f"\n{'─'*70}")
    print(f"  [Phase 3] Combined Portfolio -- Trade-Level MC")
    print(f"{'─'*70}")

    all_combined_trades = []
    for name, rtrades in all_rtrades.items():
        all_combined_trades.extend(rtrades)

    if len(all_combined_trades) >= 5:
        combined_mc = monte_carlo_trade_sim(all_combined_trades, total_capital, target_days=args.mc, sims=5000)
        if combined_mc:
            print(f"\n  Combined {args.mc}d simulation (5000 sims):")
            print(f"    Median final: ${combined_mc['median_final']:,.0f}  |  Median return: {combined_mc['median_return_pct']:+.1f}%")
            print(f"    Mean return: {combined_mc['mean_return_pct']:+.1f}%")
            print(f"    Bankrupt risk: {combined_mc['bankrupt_pct']:.1f}%")
            print(f"    50% drawdown risk: {combined_mc['ruin_50pct_pct']:.1f}%")
            print(f"    Avg drawdown: {combined_mc['avg_drawdown_pct']:.1f}%")

            print(f"\n  5-Scenario Return Distribution:")
            max_bar = max(abs(combined_mc['scenarios']['best']), abs(combined_mc['scenarios']['disaster']), 100)
            for label, pct in [("Bull P95", combined_mc['scenarios']['best']),
                               ("Base P50", combined_mc['scenarios']['base']),
                               ("Mild P25", combined_mc['scenarios']['bear']),
                               ("Bear P05", combined_mc['scenarios']['worst']),
                               ("Disaster P01", combined_mc['scenarios']['disaster'])]:
                print(print_bar(label, pct, max_bar))
    else:
        combined_mc = None

    # ========================================
    # Phase 4: Stress Scenarios
    # ========================================
    print(f"\n{'─'*70}")
    print(f"  [Phase 4] Stress Tests -- Extreme Scenarios")
    print(f"{'─'*70}")

    for sname, sparams in STRESS_SCENARIOS.items():
        result = run_stress_scenario(sname, sparams, all_rtrades, CAP)
        if result:
            final_val = result['median_final']
            ret_pct = result['median_return_pct']
            ruin = result['bankrupt_pct']
            status = "PASS" if ret_pct > 100 and ruin < 5 else ("WARN" if ret_pct > 0 else "FAIL")
            print(f"\n  [{status}] {sname}:")
            print(f"    Median final: ${final_val:,.0f}  |  Median return: {ret_pct:+.1f}%")
            print(f"    P05 bear: ${result['p05_final']:,.0f}  |  P01 disaster: ${result['p01_final']:,.0f}")
            print(f"    Bankrupt risk: {ruin:.1f}%  |  50% drawdown: {result['ruin_50pct_pct']:.1f}%")
            for label, pct in [("Bull P95", result['scenarios']['best']),
                               ("Base P50", result['scenarios']['base']),
                               ("Bear P05", result['scenarios']['worst'])]:
                maxb = max(abs(result['scenarios']['best']), abs(result['scenarios']['worst']), 50)
                print(print_bar(label, pct, maxb))

    # ========================================
    # Phase 5: Summary
    # ========================================
    print(f"\n{'='*70}")
    print(f"  [Summary] Risk Metrics")
    print(f"{'='*70}")

    if combined_mc:
        calmar = abs(combined_mc['median_return_pct']) / max(combined_mc['avg_drawdown_pct'], 0.1)
        print(f"  Expected return (median):  {combined_mc['median_return_pct']:+.1f}%")
        print(f"  Expected return (mean):    {combined_mc['mean_return_pct']:+.1f}%")
        print(f"  Bankrupt risk:             {combined_mc['bankrupt_pct']:.1f}%")
        print(f"  50% drawdown risk:         {combined_mc['ruin_50pct_pct']:.1f}%")
        print(f"  Avg max drawdown:          {combined_mc['avg_drawdown_pct']:.1f}%")
        print(f"  Calmar ratio:              {calmar:.1f}x (>2 = excellent)")
        print(f"  80% confidence interval:   {combined_mc['scenarios']['worst']:+.1f}% ~ {combined_mc['scenarios']['best']:+.1f}%")

    # Save detailed results
    os.makedirs(RESULT_DIR, exist_ok=True)
    out = os.path.join(RESULT_DIR, f"summary_{CAP}.json")
    save_data = {}
    for name, r in all_stats.items():
        save_data[name] = {k: v for k, v in r.items() if k not in ('equity', 'daily_returns')}
    if combined_mc:
        save_data['combined_mc'] = combined_mc
    with open(out, 'w') as f:
        json.dump(save_data, f, indent=2, default=str)
    print(f"\n  Details saved: {out}")
    print(f"{'='*70}")


if __name__ == '__main__':
    main()
