"""
Polymarket Copy-Trading Bot — Monthly Wallet Selector
=====================================================
Fetches leaderboard data, scores traders by trading frequency + ROI + risk control,
runs backtest validation, and selects the top 3-5 wallets for the bot to follow.

Usage:
    # Preview mode (recommended first run)
    python wallet_selector.py --preview

    # Apply mode (updates .env with selected wallets)
    python wallet_selector.py --apply

    # Custom candidate pool size
    python wallet_selector.py --preview --top-n 10 --candidates 30

    # Skip backtest validation (faster but less reliable)
    python wallet_selector.py --preview --no-backtest

Scoring Formula:
    score = frequency_score(30%) + roi_score(50%) + risk_score(20%) - loss_penalty
Backtest Filter:
    Candidates must pass a 30-day historical backtest (positive return required).
"""

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    import requests
except ImportError:
    print("请先安装 requests: pip install requests")
    sys.exit(1)

try:
    from dotenv import load_dotenv, set_key as dotenv_set_key
except ImportError:
    print("请先安装 python-dotenv: pip install python-dotenv")
    sys.exit(1)

# Backtest engine for pre-selection validation
try:
    from backtest import BacktestEngine
    from config import USER_CAPITAL, COPY_MULTIPLIER, MAX_POSITION_PCT, DEFAULT_TRADER_CAPITAL
except ImportError:
    BacktestEngine = None
    USER_CAPITAL = 1000
    COPY_MULTIPLIER = 1.0
    MAX_POSITION_PCT = 0.08
    DEFAULT_TRADER_CAPITAL = 10000

# ── API Endpoints (mirrors config.py) ──
DATA_API = "https://data-api.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"

# ── Scoring Weights ──
W_FREQUENCY = 0.30   # 30%
W_ROI = 0.50         # 50%
W_RISK = 0.20        # 20%

# ── Thresholds ──
MIN_TRADES_MONTH = 20        # 最低月交易笔数
MIN_VOLUME_USD = 10_000      # 最低月交易量
MAX_SELECT = 5               # 最多选多少人
MIN_SELECT = 3               # 最少选多少人
DEFAULT_CANDIDATES = 50      # 默认候选池大小

# ── Risk Penalty ──
MAX_SINGLE_LOSS_PCT = 0.20   # 单笔亏损超过组合 20% 即触发惩罚
PENALTY_FACTOR = 0.30        # 触发惩罚后扣减 30% 总分


def fetch_leaderboard(
    category: str = "OVERALL",
    time_period: str = "MONTH",
    order_by: str = "PNL",
    limit: int = 50,
) -> list[dict]:
    """Fetch leaderboard from Polymarket data API."""
    url = f"{DATA_API}/v1/leaderboard"
    params = {
        "category": category,
        "timePeriod": time_period,
        "orderBy": order_by,
        "limit": limit,
    }
    try:
        resp = requests.get(url, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        traders = []
        for entry in data:
            traders.append({
                "rank": entry.get("rank", 0),
                "wallet": entry.get("proxyWallet", "").lower(),
                "username": entry.get("userName", "") or entry.get("xUsername", ""),
                "display_name": entry.get("userName", "") or f"Trader #{entry.get('rank', 0)}",
                "pnl": float(entry.get("pnl", 0)),
                "volume": float(entry.get("vol", 0)),
                "x_username": entry.get("xUsername", ""),
                "verified": entry.get("verifiedBadge", False),
                "win_rate": float(entry.get("winRate", 0)) if entry.get("winRate") else None,
                "trades_count": int(entry.get("numTrades", 0)) if entry.get("numTrades") else None,
            })
        return traders
    except requests.exceptions.RequestException as e:
        print(f"  ⚠️  排行榜 API 请求失败: {e}")
        return []
    except (ValueError, TypeError, KeyError) as e:
        print(f"  ⚠️  排行榜数据解析失败: {e}")
        return []


def fetch_trader_trades(
    wallet: str, limit: int = 100
) -> list[dict]:
    """Fetch recent trades for a wallet."""
    url = f"{DATA_API}/trades"
    params = {"user": wallet, "limit": limit, "takerOnly": True}
    try:
        resp = requests.get(url, params=params, timeout=15)
        resp.raise_for_status()
        trades = []
        for t in resp.json():
            trades.append({
                "timestamp": int(t.get("timestamp", 0)),
                "side": t.get("side", ""),
                "size": float(t.get("size", 0)),
                "price": float(t.get("price", 0)),
                "outcome": t.get("outcome", ""),
                "title": t.get("title", ""),
            })
        return trades
    except requests.exceptions.RequestException as e:
        print(f"      ⚠️  交易历史获取失败: {e}")
        return []
    except (ValueError, TypeError) as e:
        print(f"      ⚠️  交易历史解析失败: {e}")
        return []


def fetch_trader_positions(
    wallet: str, size_threshold: int = 10
) -> list[dict]:
    """Fetch open positions for a wallet."""
    url = f"{DATA_API}/positions"
    params = {"user": wallet, "sizeThreshold": size_threshold, "limit": 500}
    try:
        resp = requests.get(url, params=params, timeout=15)
        resp.raise_for_status()
        positions = []
        for p in resp.json():
            positions.append({
                "initial_value": float(p.get("initialValue", 0)),
                "current_value": float(p.get("currentValue", 0)),
                "cash_pnl": float(p.get("cashPnl", 0)),
                "percent_pnl": float(p.get("percentPnl", 0)),
                "realized_pnl": float(p.get("realizedPnl", 0)),
                "title": p.get("title", ""),
                "size": float(p.get("size", 0)),
            })
        return positions
    except Exception:
        return []


def calc_trade_frequency(trades: list[dict], current_ts: int | None = None) -> int:
    """Count trades in the last 30 days."""
    if not trades:
        return 0
    now = current_ts or int(time.time())
    thirty_days_ago = now - 30 * 86400
    recent = [t for t in trades if t.get("timestamp", 0) >= thirty_days_ago]
    return len(recent)


def calc_roi(
    leaderboard_entry: dict, positions: list[dict]
) -> tuple[float, float]:
    """
    Calculate ROI and total PnL.
    Returns (roi_pct, total_pnl).
    roi_pct = PnL / Volume (as decimal, e.g. 0.15 = 15%).
    """
    pnl = leaderboard_entry.get("pnl", 0)
    volume = leaderboard_entry.get("volume", 0)

    # Also try realized PnL from positions
    realized_pnl = sum(p.get("realized_pnl", 0) for p in positions if p.get("realized_pnl", 0) != 0)
    total_pnl = pnl + realized_pnl

    if volume > 0:
        roi = total_pnl / volume
    else:
        roi = 0.0

    return roi, total_pnl


def calc_risk_score(trades: list[dict], total_capital: float) -> tuple[float, float, int]:
    """
    Calculate risk-related metrics based on historical trade data.
    Uses position-level PnL estimation from trade history.
    Returns (max_single_loss_pct, avg_loss_pct, num_losing_trades).
    """
    if not trades or len(trades) < 5:
        return (0.0, 0.0, 0)

    # Sort by timestamp to ensure chronological order
    sorted_trades = sorted(trades, key=lambda x: x.get("timestamp", 0))

    # Track positions by asset
    positions: dict[str, dict] = {}
    position_pnls: list[float] = []

    for t in sorted_trades:
        side = t.get("side", "").upper()
        size = float(t.get("size", 0))
        price = float(t.get("price", 0))
        asset = t.get("asset", "") or t.get("condition_id", "")

        if not asset or size <= 0 or price <= 0:
            continue

        if side == "BUY":
            # Increase position
            cost = size * price
            if asset in positions:
                p = positions[asset]
                total_shares = p["shares"] + size
                total_cost = p["shares"] * p["avg_price"] + cost
                p["shares"] = total_shares
                p["avg_price"] = total_cost / total_shares
            else:
                positions[asset] = {"shares": size, "avg_price": price}

        elif side == "SELL":
            if asset not in positions:
                continue
            p = positions[asset]
            sell_shares = min(size, p["shares"])
            if sell_shares <= 0:
                continue
            # Realized PnL for this partial sell
            pnl = sell_shares * (price - p["avg_price"])
            pnl_pct = pnl / (sell_shares * p["avg_price"]) * 100 if p["avg_price"] > 0 else 0
            position_pnls.append(pnl_pct)
            p["shares"] -= sell_shares
            if p["shares"] < 0.001:
                del positions[asset]

    # For remaining open positions, estimate unrealized PnL
    for asset, p in positions.items():
        if p["shares"] > 0 and p["avg_price"] > 0:
            # Use last trade price of this asset as estimate
            last_trades = [t for t in sorted_trades if (
                (t.get("asset", "") or t.get("condition_id", "")) == asset
            )]
            if last_trades:
                last_price = float(last_trades[-1].get("price", p["avg_price"]))
                unrealized_pnl_pct = (last_price - p["avg_price"]) / p["avg_price"] * 100
                position_pnls.append(unrealized_pnl_pct)

    if not position_pnls:
        return (0.0, 0.0, 0)

    losing_pnls = [x for x in position_pnls if x < 0]
    max_single_loss = abs(min(position_pnls)) if position_pnls else 0.0
    avg_loss = sum(losing_pnls) / len(losing_pnls) if losing_pnls else 0.0

    return (max_single_loss, abs(avg_loss), len(losing_pnls))


def calc_risk_from_positions(positions: list[dict]) -> tuple[float, float, int]:
    """
    Calculate risk from position data.
    Returns (max_percent_pnl_loss, avg_loss_pct, num_losing_positions).
    """
    if not positions:
        return (0.0, 0.0, 0)

    losing_positions = []
    all_pnl_pcts = []

    for p in positions:
        pnl_pct = p.get("percent_pnl", 0)
        if pnl_pct != 0:
            all_pnl_pcts.append(pnl_pct)
        if pnl_pct < -5:  # More than 5% loss
            losing_positions.append(pnl_pct)

    if not all_pnl_pcts:
        return (0.0, 0.0, 0)

    max_loss = min(all_pnl_pcts)  # Most negative
    avg_loss = sum(losing_positions) / len(losing_positions) if losing_positions else 0.0

    return (abs(max_loss), abs(avg_loss), len(losing_positions))


def truncate_wallet(wallet: str, chars: int = 12) -> str:
    """Truncate wallet address for display."""
    if len(wallet) <= chars:
        return wallet
    return wallet[:6] + "..." + wallet[-4:]


class WalletSelector:
    """Main selection engine: scores traders and picks the best ones."""

    def __init__(self, candidate_count: int = DEFAULT_CANDIDATES, skip_backtest: bool = False):
        self.candidate_count = candidate_count
        self.skip_backtest = skip_backtest
        self.candidates: list[dict[str, Any]] = []
        self.selected: list[dict[str, Any]] = []
        self.max_frequency = 0
        self.max_roi = 0.001  # Avoid division by zero

    def run(self) -> list[dict[str, Any]]:
        """Full pipeline: fetch → enrich → score → select."""
        print(f"\n{'='*60}")
        print(f"  🏛️  Poly_Copy — 月度跟单对象选择器")
        print(f"  📅 {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
        print(f"  📊 候选池: Top {self.candidate_count}")
        print(f"{'='*60}")

        # Step 1: Fetch leaderboard
        print(f"\n📡 正在获取排行榜 Top {self.candidate_count}...")
        leaderboard = fetch_leaderboard(limit=self.candidate_count)
        if not leaderboard:
            print("  ❌ 无法获取排行榜数据")
            return []
        print(f"  ✅ 获取到 {len(leaderboard)} 个交易员")

        # Step 2: Filter & Enrich
        print(f"\n🔍 正在过滤和补充数据（可能会花一些时间）...")
        enriched = []

        for i, trader in enumerate(leaderboard, 1):
            wallet = trader["wallet"]
            volume = trader["volume"]
            username = trader["display_name"] or truncate_wallet(wallet)

            # Quick filter: minimum volume
            if volume < MIN_VOLUME_USD:
                continue

            print(f"  [{i}/{len(leaderboard)}] {username:20s} ({truncate_wallet(wallet)})...", end=" ")

            # Fetch trades
            trades = fetch_trader_trades(wallet, limit=200)
            freq = calc_trade_frequency(trades)

            # Quick filter: minimum trades (skip API-heavy traders early)
            if len(trades) == 0:
                print("⏭️  无交易记录")
                continue
            if freq < MIN_TRADES_MONTH:
                print(f"⏭️  交易不足 ({freq}/{MIN_TRADES_MONTH})")
                continue

            # Fetch positions for risk analysis
            positions = fetch_trader_positions(wallet)

            # Calculate metrics
            roi, total_pnl = calc_roi(trader, positions)
            max_loss_pct, avg_loss_pct, num_losing = calc_risk_from_positions(positions)

            # Track max values for normalization
            self.max_frequency = max(self.max_frequency, freq)
            self.max_roi = max(self.max_roi, abs(roi))

            entry = {
                "wallet": wallet,
                "username": trader.get("username", ""),
                "display_name": trader.get("display_name", ""),
                "rank": trader["rank"],
                "pnl": trader["pnl"],
                "volume": volume,
                "freq": freq,
                "roi": roi,
                "total_pnl": total_pnl,
                "max_loss_pct": max_loss_pct,
                "avg_loss_pct": avg_loss_pct,
                "num_losing_positions": num_losing,
                "win_rate": trader.get("win_rate"),
                "trades_count": trader.get("trades_count"),
                "verified": trader.get("verified", False),
            }
            enriched.append(entry)
            print(f"✅  {freq}笔/月 | ROI {roi*100:+.1f}%")

            # Rate limit: be nice to Polymarket API
            time.sleep(0.2)

        self.candidates = enriched
        print(f"\n📋 候选池初步筛选结果: {len(self.candidates)} 人通过")

        if not self.candidates:
            print("  ⚠️  无符合条件的交易员，请放宽筛选条件")
            return []

        # Step 3: Score
        self._score_all()

        # Step 4: Backtest validation (new!)
        if not self.skip_backtest:
            self._backtest_filter()

        # Step 5: Select
        self._select_top()

        return self.selected

    def _score_all(self):
        """Score all candidates."""
        for c in self.candidates:
            c["score"] = self._calc_score(c)

        # Sort by score descending
        self.candidates.sort(key=lambda x: x["score"], reverse=True)

    def _calc_score(self, candidate: dict) -> float:
        """Calculate composite score for a single candidate."""
        freq = candidate["freq"]
        roi = candidate["roi"]
        max_loss_pct = candidate["max_loss_pct"]
        num_losing = candidate["num_losing_positions"]

        # 1) Frequency score (0-100, scaled against max in pool)
        freq_score = (freq / self.max_frequency) * 100 if self.max_frequency > 0 else 0
        freq_score = min(100, freq_score)

        # 2) ROI score (0-100)
        # Positive ROI is good, negative is bad
        if roi >= 0:
            # Scale: 0% → 50, 20%+ → 100
            roi_score = min(100, 50 + (roi / 0.20) * 50)
        else:
            # Negative ROI: scale down rapidly
            roi_score = max(0, 50 + (roi / 0.20) * 50)

        # 3) Risk score (0-100)
        # Lower max loss = better
        if max_loss_pct <= 0:
            risk_score = 100  # No losses recorded
        else:
            # 0% loss → 100, 50% loss → 0
            risk_score = max(0, 100 - (max_loss_pct / 0.50) * 100)

        # 4) Loss penalty
        penalty = 0.0
        # Only apply if loss is in reasonable range (1-99%, not 0 or 100 which are API artifacts)
        if 1 < max_loss_pct < 99 and max_loss_pct > MAX_SINGLE_LOSS_PCT * 100:
            penalty = PENALTY_FACTOR * 100  # Flat 30% deduction

        # 5) Verified badge bonus
        verified_bonus = 5.0 if candidate.get("verified") else 0.0

        # 6) ROI sanity cap
        #   ROI > 50% is suspicious (small base / lucky trade), penalize for stability
        roi_cap_penalty = 0.0
        if roi > 0.50:
            roi_cap_penalty = min(15, (roi - 0.50) * 20)

        composite = (
            freq_score * W_FREQUENCY
            + roi_score * W_ROI
            + risk_score * W_RISK
            - penalty
            - roi_cap_penalty
            + verified_bonus
        )

        return round(max(0, composite), 1)

    # ------------------------------------------------------------------
    # Backtest validation (P0)
    # ------------------------------------------------------------------
    def _fetch_trades_for_backtest(self, wallet: str, limit: int = 500) -> list[dict]:
        """Fetch extended trade history for backtest validation."""
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
                })
            trades.sort(key=lambda x: x["timestamp"])
            return trades
        except Exception as e:
            print(f"      ⚠️  回测数据获取失败: {e}")
            return []

    def _backtest_candidate(self, candidate: dict, capital: int = 1000) -> dict | None:
        """Run a quick backtest for a single candidate. Returns stats dict or None."""
        if BacktestEngine is None:
            print("      ⚠️  回测引擎不可用，跳过验证")
            return None

        wallet = candidate["wallet"]
        trades = self._fetch_trades_for_backtest(wallet, limit=500)

        if len(trades) < 30:
            print(f"      ⚠️  交易不足30笔，无法回测")
            return None

        # Estimate portfolio: use leaderboard volume as proxy, or default
        portfolio_est = max(candidate.get("volume", 0) / 10, DEFAULT_TRADER_CAPITAL)
        if portfolio_est < 1000:
            portfolio_est = DEFAULT_TRADER_CAPITAL

        try:
            engine = BacktestEngine(
                trader_name=candidate["display_name"],
                capital=capital,
                trades=trades,
                trader_portfolio=portfolio_est,
                wallet=wallet,
            )
            engine.run()
            stats = engine.stats()
            return stats
        except Exception as e:
            print(f"      ⚠️  回测异常: {e}")
            return None

    def _backtest_filter(self):
        """Run backtest validation on top-scored candidates and filter out losers.

        Only backtests the top 15 candidates (by score) to limit API usage.
        Filters out any candidate whose backtest shows negative total return.
        """
        if not self.candidates:
            return

        top_n = min(15, len(self.candidates))
        to_test = self.candidates[:top_n]

        print(f"\n{'─'*60}")
        print(f"  🧪 回测验证 — 对前 {len(to_test)} 名候选人运行历史回测")
        print(f"  (策略: $1,000本金, {COPY_MULTIPLIER}x倍率, {MAX_POSITION_PCT*100:.0f}%仓位上限)")
        print(f"{'─'*60}")

        passed = []
        failed = []

        for i, c in enumerate(to_test, 1):
            name = c["display_name"] or c["wallet"][:10]
            print(f"  [{i}/{len(to_test)}] {name:20s}...", end=" ", flush=True)

            stats = self._backtest_candidate(c, capital=USER_CAPITAL)

            if stats is None:
                # Unable to backtest — keep candidate but mark as unverified
                c["backtest_verified"] = False
                passed.append(c)
                print("⏭️  跳过（数据不足）")
            elif stats["return_pct"] > 0:
                c["backtest_verified"] = True
                c["backtest_return"] = stats["return_pct"]
                c["backtest_pf"] = stats["profit_factor"]
                c["backtest_dd"] = stats["max_dd_pct"]
                passed.append(c)
                print(f"✅ +{stats['return_pct']:.1f}% | PF={stats['profit_factor']:.1f} | DD={stats['max_dd_pct']:.0f}%")
            else:
                c["backtest_verified"] = False
                c["backtest_return"] = stats["return_pct"]
                failed.append(c)
                print(f"❌ {stats['return_pct']:+.1f}% — 淘汰")

            time.sleep(0.3)  # Rate limit

        # Update candidates: keep passed + untested (lower-scored)
        untested = self.candidates[top_n:]
        self.candidates = passed + untested

        n_passed = len(passed)
        n_failed = len(failed)
        n_untested = len(untested)

        print(f"\n  📊 回测结果: {n_passed}通过 | {n_failed}淘汰 | {n_untested}未测试(保留)")
        if failed:
            print(f"  淘汰名单: {', '.join(c['display_name'][:16] or c['wallet'][:10] for c in failed)}")

    def _select_top(self):
        """Select top traders, ensuring min/max constraints.

        Prefers backtest-verified candidates. Falls back to unverified only if
        not enough verified candidates pass.
        """
        verified = [c for c in self.candidates if c.get("backtest_verified") and c["score"] > 0]
        unverified = [c for c in self.candidates if not c.get("backtest_verified") and c["score"] > 0]

        # Prefer verified, then fill with unverified
        selected = verified[:MAX_SELECT]

        if len(selected) < MIN_SELECT:
            needed = MIN_SELECT - len(selected)
            fill = unverified[:needed]
            selected.extend(fill)
            if fill:
                names = [c["display_name"][:14] or c["wallet"][:10] for c in fill]
                print(f"  ⚠️  未通过回测但仍入选（人数不足）: {', '.join(names)}")

        if len(selected) < MIN_SELECT:
            # Still not enough — relax to any positive-score candidate
            all_ok = [c for c in self.candidates if c["score"] > 0]
            selected = all_ok[:max(MIN_SELECT, 1)]

        self.selected = selected

    def print_results(self):
        """Print selection results."""
        print(f"\n{'='*60}")
        print(f"  📋 筛选结果")
        print(f"{'='*60}")

        if not self.selected:
            print("  ❌ 未选出任何交易员")
            return

        print(f"\n  🏆 推荐跟单对象（共 {len(self.selected)} 人）:")
        print(f"  {'':─^60}")
        print(f"  {'#':>3} {'名称':18s} {'得分':>6} {'频次':>6} {'ROI':>10} {'月PNL':>12} {'风控':>6}")
        print(f"  {'':─^60}")

        for i, c in enumerate(self.selected, 1):
            name = (c["display_name"][:16] or truncate_wallet(c["wallet"]))
            roi_str = f"{c['roi']*100:+.1f}%"
            pnl_str = f"${c['pnl']:+,.0f}"
            risk_str = f"{c['max_loss_pct']:.0f}%" if c["max_loss_pct"] > 0 else "良好"
            print(f"  {i:>3} {name:18s} {c['score']:>6.1f} {c['freq']:>6} {roi_str:>10} {pnl_str:>12} {risk_str:>6}")

        print(f"\n  📊 详细数据:")
        for i, c in enumerate(self.selected, 1):
            print(f"\n    [{i}] {c['display_name']}")
            print(f"        钱包: {c['wallet']}")
            print(f"        排名: #{c['rank']} | 月交易: {c['freq']}笔 | 交易量: ${c['volume']:,.0f}")
            print(f"        ROI: {c['roi']*100:+.2f}% | 月PnL: ${c['pnl']:+,.0f}")
            print(f"        最大单笔亏损: {c['max_loss_pct']:.1f}% | 得分: {c['score']}")
            bt_ret = c.get("backtest_return")
            if bt_ret is not None:
                bt_pf = c.get("backtest_pf", 0)
                bt_dd = c.get("backtest_dd", 0)
                status = "✅ 通过" if c.get("backtest_verified") else "❌ 未通过"
                print(f"        回测: {status} | 收益 {bt_ret:+.1f}% | PF {bt_pf:.1f} | 回撤 {bt_dd:.0f}%")

    def get_env_wallets(self) -> str:
        """Get comma-separated wallet addresses for .env."""
        return ",".join(c["wallet"] for c in self.selected)

    def apply_to_env(self, env_path: str | None = None):
        """Update FOLLOW_WALLETS in .env file."""
        if not self.selected:
            print("  ❌ 无可用交易员，跳过 .env 更新")
            return

        if env_path is None:
            # Auto-detect: look for .env in script directory
            env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")

        if not os.path.exists(env_path):
            print(f"  ⚠️  .env 文件不存在: {env_path}")
            print(f"  请先创建 {env_path}")
            return

        new_wallets = self.get_env_wallets()
        current_wallets = os.environ.get("FOLLOW_WALLETS", "")

        if current_wallets.lower().strip(",") == new_wallets.lower().strip(","):
            print(f"\n  ℹ️  FOLLOW_WALLETS 未变化，无需更新")
            return

        # Update .env
        dotenv_set_key(env_path, "FOLLOW_WALLETS", new_wallets)
        print(f"\n  ✅ FOLLOW_WALLETS 已更新!")
        print(f"     新旧对比:")
        print(f"       旧: {current_wallets[:50]}...")
        print(f"       新: {new_wallets[:50]}...")
        print(f"     请重启 Bot 以使配置生效: sudo systemctl restart poly-bot")


def parse_args():
    parser = argparse.ArgumentParser(
        description="Poly_Copy 月度跟单对象选择器",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python wallet_selector.py --preview               # 预览模式（推荐首次运行）
  python wallet_selector.py --apply                 # 应用模式（更新 .env）
  python wallet_selector.py --preview --candidates 30 # 缩小候选范围
  python wallet_selector.py --apply --top-n 3       # 只选3人
        """,
    )
    parser.add_argument(
        "--preview",
        action="store_true",
        help="预览模式：只查看结果，不修改 .env",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="应用模式：预览 + 更新 .env",
    )
    parser.add_argument(
        "--candidates",
        type=int,
        default=DEFAULT_CANDIDATES,
        help=f"候选池大小（默认 {DEFAULT_CANDIDATES}）",
    )
    parser.add_argument(
        "--top-n",
        type=int,
        default=MAX_SELECT,
        help=f"最终选取人数（默认 {MAX_SELECT}）",
    )
    parser.add_argument(
        "--env-path",
        type=str,
        default=None,
        help=".env 文件路径（默认自动检测）",
    )
    parser.add_argument(
        "--no-backtest",
        action="store_true",
        help="跳过回测验证（更快但可靠性低）",
    )
    return parser.parse_args()


def main():
    args = parse_args()

    if not args.preview and not args.apply:
        print("请指定 --preview（预览）或 --apply（应用）模式")
        sys.exit(1)

    global MAX_SELECT
    MAX_SELECT = args.top_n

    selector = WalletSelector(candidate_count=args.candidates, skip_backtest=args.no_backtest)
    results = selector.run()

    if results:
        selector.print_results()

        if args.apply:
            env_path = args.env_path
            if env_path is None:
                # Try VPS path first, then local
                vps_env = "/opt/poly_copy/.env"
                local_env = os.path.join(
                    os.path.dirname(os.path.abspath(__file__)), ".env"
                )
                env_path = vps_env if os.path.exists(vps_env) else local_env

            selector.apply_to_env(env_path)

        # Output JSON for programmatic use
        output = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "selected_count": len(results),
            "selected": [
                {
                    "wallet": c["wallet"],
                    "display_name": c["display_name"],
                    "score": c["score"],
                    "rank": c["rank"],
                    "freq": c["freq"],
                    "roi_pct": round(c["roi"] * 100, 2),
                    "pnl": c["pnl"],
                    "volume": c["volume"],
                }
                for c in results
            ],
        }
        output_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "results",
            "wallet_selection.json",
        )
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(output, f, indent=2, ensure_ascii=False)
        print(f"\n  💾 结果已保存到: {output_path}")

        print(f"\n{'='*60}")
        print(f"  ✅ 选择完成！")
        if args.apply:
            print(f"     已更新 .env 文件，请重启机器人")
        else:
            print(f"     预览模式，如需应用请运行: python wallet_selector.py --apply")
        print(f"{'='*60}")
    else:
        print(f"\n  ❌ 选择失败，未找到符合条件的交易员")


if __name__ == "__main__":
    main()
