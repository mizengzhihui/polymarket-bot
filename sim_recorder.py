"""
Poly_Copy Bot — 模拟交易记录器 (Simulation Recorder)
与实盘并行运行，记录所有跟单信号 + 不同资本水平的模拟执行。

功能:
1. 记录每一个跟单信号（时间、交易员、币种、方向、金额）
2. 模拟 $500 / $1000 / $2000 资本水平的执行结果
3. 生成月末对比报告
"""
import json
import os
import logging
from datetime import datetime, timezone
from typing import Optional

# ============================================================
# 配置
# ============================================================

# 模拟资本水平（用于对比分析）
SIM_CAPITAL_LEVELS = [500, 1000, 2000]

# 模拟参数（与 bot.py 保持一致）
SIM_MIN_TRADE_SIZE = 4.0      # 最低跟单金额
SIM_MAX_POSITION_PCT = 0.08   # 单笔上限比例（8% 资本）
SIM_COPY_MULTIPLIER = 0.5     # 跟单倍数
SIM_MAX_POSITION_SIZE_CAP = 50  # 单笔上限（$500 资本时为 $40，但给个绝对 cap）

# 存储目录
BASE = os.path.dirname(os.path.abspath(__file__))
SIM_LOG_DIR = os.path.join(BASE, "results", "simulation")


def _ensure_dir():
    os.makedirs(SIM_LOG_DIR, exist_ok=True)


def compute_sim_copy(trade_size_usd: float, capital: int) -> dict:
    """
    对给定的资本水平计算模拟跟单结果。
    返回 {"action": "BUY"|"SKIP", "copy_usd": float, "reason": str|None}
    """
    copy_usd = trade_size_usd * SIM_COPY_MULTIPLIER
    # 按资本的比例上限
    pos_cap = capital * SIM_MAX_POSITION_PCT
    if copy_usd > pos_cap:
        copy_usd = pos_cap
    # 绝对上限
    if copy_usd > SIM_MAX_POSITION_SIZE_CAP:
        copy_usd = SIM_MAX_POSITION_SIZE_CAP
    # 最低交易阈值
    if copy_usd < SIM_MIN_TRADE_SIZE:
        return {"action": "SKIP", "copy_usd": 0, "reason": "below_min_trade"}
    return {"action": "BUY", "copy_usd": round(copy_usd, 2), "reason": None}


def record_signal(
    trader_wallet: str,
    asset: str,
    side: str,
    size_usd: float,
    price: float,
    real_copy_usd: float,
    real_action: str,
    real_reason: Optional[str] = None,
):
    """
    记录一个跟单信号及其模拟数据。

    参数:
        trader_wallet: 交易员钱包地址
        asset: 资产 ID
        side: BUY / SELL
        size_usd: 原始交易金额 (USD)
        price: 成交价格
        real_copy_usd: 实盘跟单金额 (0 表示跳过)
        real_action: 实盘动作 ("BUY"|"SKIP")
        real_reason: 跳过原因（可选）
    """
    _ensure_dir()

    # 每天一个 JSONL 文件
    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    log_file = os.path.join(SIM_LOG_DIR, f"signals_{today}.jsonl")

    # 对所有模拟资本水平计算
    sim_results = {}
    for cap in SIM_CAPITAL_LEVELS:
        result = compute_sim_copy(size_usd, cap)
        sim_results[str(cap)] = result

    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "trader": trader_wallet[:10] + "...",
        "asset": asset[:12] + "...",
        "side": side,
        "size_usd": round(size_usd, 2),
        "price": round(price, 6),
        "real": {
            "capital": 47,  # 当前实盘资本
            "action": real_action,
            "copy_usd": round(real_copy_usd, 2),
            "reason": real_reason,
        },
        "simulations": sim_results,
    }

    with open(log_file, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")

    return record


def _load_all_signals() -> list[dict]:
    """加载所有 signals_*.jsonl 文件，返回记录列表。"""
    _ensure_dir()
    records = []
    for fname in sorted(os.listdir(SIM_LOG_DIR)):
        if fname.startswith("signals_") and fname.endswith(".jsonl"):
            fpath = os.path.join(SIM_LOG_DIR, fname)
            try:
                with open(fpath, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line:
                            records.append(json.loads(line))
            except Exception as e:
                print(f"  [SIM] Error reading {fname}: {e}")
    return records


def generate_monthly_report() -> str:
    """
    生成月末模拟对比报告。
    统计各资本水平的：总信号数、可跟单数、跳过数、模拟投入额。
    与 $47 实盘对比。
    返回 Markdown 格式报告字符串。
    """
    records = _load_all_signals()
    if not records:
        return "# 模拟交易报告\n\n暂无数据。一个月后再来查看。\n"

    total = len(records)

    # 统计：实盘
    real_buys = sum(1 for r in records if r["real"]["action"] == "BUY")
    real_skips = total - real_buys
    real_total_usd = sum(r["real"]["copy_usd"] for r in records if r["real"]["action"] == "BUY")

    # 统计：各模拟水平
    levels_stats = {}
    for cap in SIM_CAPITAL_LEVELS:
        sk = str(cap)
        buys = sum(1 for r in records if r["simulations"].get(sk, {}).get("action") == "BUY")
        skips = total - buys
        total_usd = sum(
            r["simulations"].get(sk, {}).get("copy_usd", 0)
            for r in records
            if r["simulations"].get(sk, {}).get("action") == "BUY"
        )
        levels_stats[cap] = {
            "buys": buys,
            "skips": skips,
            "total_usd": total_usd,
        }

    # 生成报告
    lines = [
        "# 📊 模拟交易月度对比报告",
        "",
        f"**统计周期:** {records[0]['ts'][:10]} ~ {records[-1]['ts'][:10]}",
        f"**总跟单信号数:** {total}",
        "",
        "---",
        "",
        "## 对比总览",
        "",
        "| 资本水平 | 可跟单数 | 跳过数 | 总投入(USD) | 跟单率 |",
        "|---------|---------|-------|------------|-------|",
        f"| **$47 (实盘)** | {real_buys} | {real_skips} | ${real_total_usd:.2f} | {real_buys/total*100:.1f}% |",
    ]

    for cap in SIM_CAPITAL_LEVELS:
        s = levels_stats[cap]
        lines.append(
            f"| **${cap}** | {s['buys']} | {s['skips']} | ${s['total_usd']:.2f} | {s['buys']/total*100:.1f}% |"
        )

    lines += [
        "",
        "## 分析说明",
        "",
        "- **$47 实盘**: 当前实际资金，受 $4 最低跟单金额限制，许多小额信号被跳过。",
        "- **$500 模拟**: 更多信号可跟，资金利用率更高。",
        "- **$1000 模拟**: 可跟几乎所有信号，分散风险。",
        "- **$2000 模拟**: 资金充裕，可同时持有多个仓位。",
        "",
        "---",
        f"*报告生成时间: {datetime.now(timezone.utc).isoformat()}*",
        "",
    ]

    report = "\n".join(lines)

    # 写入文件
    report_path = os.path.join(SIM_LOG_DIR, "monthly_report.md")
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(report)

    return report
