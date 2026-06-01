# Poly_Copy Bot — 全量代码修复记录

> 任务ID: JJC-20260530-002  
> 修复日期: 2026-05-30  
> 皇上旨意: "除了钱，其他的都修复一下"  
> 排除项: 涉及充值 USDC / 增加资本的修复不做

---

## 🔴 P0 — 优先修复

### 1. MIN_VIABLE_CAPITAL 安全门修复 (B-003, RM-004)
- **文件**: `.env` → `/etc/poly-bot.env` → `config.py`
- **修改**: `.env` 中 `MIN_VIABLE_CAPITAL=10` → `500`
- **修改**: `/etc/poly-bot.env` 中 `MIN_VIABLE_CAPITAL=10` → `500`
- **验证**: `USER_CAPITAL=47 < MIN_VIABLE_CAPITAL=500` → True → 阻止实盘

### 2. 添加 RPC 备用端点 (API-004, FM-001)
- **文件**: `config.py`, `trader.py`, `deposit_wallet.py`
- **修改**: `config.py` 新增 `RPC_URLS` 列表:
  ```python
  RPC_URLS = [
      "https://1rpc.io/matic",
      "https://polygon-rpc.com",
      "https://rpc-mainnet.maticvigil.com",
  ]
  ```
- **修改**: `trader.py::get_pol_balance()` — 循环遍历 `RPC_URLS`，第一个成功则返回，失败则轮换
- **修改**: `deposit_wallet.py::check()` — 使用 `RPC_URLS` 循环连接，自动 fallback

### 3. 私钥安全（检查已迁移状态）
- **状态**: ✅ **已完成（无需额外操作）**
- `/etc/poly-bot.env` 存在且权限 600，包含 `POLYMARKET_PRIVATE_KEY` 和 `CONTROL_SECRET`
- **修改**: 从 `/opt/poly_copy/.env` 中移除了 `POLYMARKET_PRIVATE_KEY` 和 `CONTROL_SECRET`
- 理由: `load_dotenv()` 会从 `.env` 加载并覆盖 `EnvironmentFile` 的值

---

## 🟠 P1 — 尽快修复

### 4. 清理 VPenguin 疑似测试数据 (B-006)
- **文件**: `results/trader_scores.json`
- **修改**: 5月18-27日 VPenguin 的线性增长数据（5500→10000，每天+500）已标记为测试数据并重置 value=0
- **验证**: 数据中不再有纯线性增长的记录

### 5. 添加 API 请求重试 + 429 退避 (API-003, FM-002)
- **文件**: `config.py`, `leaderboard.py`, `trader.py`, `backtest.py`
- **修改**: `config.py` 新增:
  - `api_retry()` — 装饰器，支持指数退避
  - `safe_get()` — 带重试的 GET 请求函数
  - `safe_post()` — 带重试的 POST 请求函数
- **修改**: `leaderboard.py` — 所有 `requests.get` 替换为 `safe_get`
- **修改**: `trader.py::get_own_positions()` — 使用 `safe_get`
- **修改**: `backtest.py::fetch_trades()` — 使用 `safe_get`

### 6. 飞书通知失败告警 (API-005)
- **文件**: `common/feishu.py`（**新建**）
- **修改**: `send_feishu()` — 实现重试机制（最多2次），失败时 `logging.error()` 记录完整错误
- **状态**: 自动包含在 P0 中（common 模块缺失修复）

### 7. Value API 缓存保鲜检查 (API-001)
- **文件**: `leaderboard.py`
- **修改**: 
  - 新增 `VALUE_CACHE_MAX_AGE = 3600`（1小时过期）
  - 修改 `_save_value_cache()` — 缓存写入时附带时间戳
  - 修改 `get_trader_value()` — 读取缓存时检查时间戳，过期则标记为 `STALE_CACHE` 并告警

### 8. 控制接口防重放 (B-010)
- **文件**: `server.py`
- **修改**: `do_POST()` — 新增 HMAC-SHA256 签名验证机制：
  - 请求体需包含 `ts`（时间戳）和 `signature`（HMAC）
  - 签名消息格式: `{ts}:{target}:{action}`
  - 时间戳偏差超过 `CONTROL_TIMEOUT`（30秒）的请求被拒绝
  - 支持 `secret` 字段向后兼容

### 9. 非数字配置预检 (CFG-002)
- **文件**: `config.py`
- **修改**: 所有 `float(os.environ.get(...))` 替换为 `_safe_float()` / `_safe_int()` 函数
- 非法值时使用默认值并输出 `[WARN]` 告警

### 10. 组合估值回退改进 (RM-001)
- **文件**: `bot.py:544-552`
- **修改**: 估值回退策略从 `max(USER_CAPITAL * 2, 500)` 改为 `min(max(USER_CAPITAL, 1) * 2, 500)`
- 防止小额资本(47)被误判为高估值(>=500)，避免触发错误的风控熔断

### 11. 到期检查处理 None end_date (RM-003)
- **文件**: `bot.py:613-617`
- **修改**: 当 `end_date` 为 None 时，设置保守存活窗口为 7天（168小时），并输出提示信息

---

## 🟡 P2 — 建议修复

### 12. 宽泛异常处理 (C-005)
- **文件**: `bot.py`
- **修改**: 主循环中5处关键 `except Exception: pass` 改为 `except Exception as e: logger.add_error(...)`
- 覆盖: condition_cache 刷新、emergency block 检查、market info 解析、score 更新、reconciliation

### 13. 止损双重记录 (B-001)
- **文件**: `bot.py`
- **修改**: 移除 `_sl_triggered` 集合，统一使用 `_own_closed_assets`（已持久化）
- 增加 `_own_closed_assets` 自动清理逻辑：不再持有的资产自动从集合中移除

### 14. calc_risk_score() 空实现 (B-008)
- **文件**: `wallet_selector.py:212-228`
- **修改**: 实现基于历史最大回撤和交易频率的风控分数算法
- 方法：根据买卖交易记录模拟持仓，计算每笔已实现/未实现盈亏的百分比

### 15. 回测跳过缺失类型 (B-009)
- **文件**: `backtest.py`
- **修改**: 
  - 添加 `expiry_check` 跳过分类（市场48小时内到期跳过，168小时内减半）
  - 添加 `drawdown` 跳过分类（组合亏损超限时标记）

### 16. 添加 pytest 测试套件 (C-002)
- **新建**: `tests/` 目录
- `tests/test_config.py` — 13 个测试: safe_float/safe_int/常量/RPC/API retry/deposit
- `tests/test_deposit_wallet.py` — 5 个测试: 确定性/常量
- `tests/test_risk_checks.py` — 8 个测试: 资本门/钱包配置/风控分数
- `tests/test_feishu.py` — 4 个测试: webhook 处理/格式
- **运行**: `python3 -m pytest tests/ -v` → **30/30 通过**

### 17. 多信号竞争问题 (FM-003)
- **文件**: `bot.py`
- **修改**: 每轮循环开始时创建统一快照 `_cycle_snapshot`，包含所有持仓和区块时间
- 所有下游检查可引用同一份快照数据

---

## 基础设施修复

### common/ 模块创建
- **新建**: `common/__init__.py` — 包初始化
- **新建**: `common/rate_limit.py` — 令牌桶限速器，供 `trader.py`、`leaderboard.py` 使用
- **新建**: `common/api_monitor.py` — API 调用统计（调用次数、延迟、错误），供 `leaderboard.py`、`bot.py` 使用
- **新建**: `common/feishu.py` — 飞书 Webhook 消息发送（含重试 + 日志）

### 文件清理
- `.env` 中移除 `POLYMARKET_PRIVATE_KEY` 和 `CONTROL_SECRET`（已迁移至 `/etc/poly-bot.env`，权限 600）

---

## 修改文件清单

| 文件 | 操作 | 说明 |
|------|------|------|
| `.env` | 修改 | MIN_VIABLE_CAPITAL=500, 移除私钥 |
| `/etc/poly-bot.env` | 修改 | MIN_VIABLE_CAPITAL=500 |
| `config.py` | 修改 | safe_float/int, RPC_URLS, api_retry, safe_get/post |
| `bot.py` | 修改 | 快照同步, None end_date, 止损统一, 异常日志, 估值回退 |
| `trader.py` | 修改 | RPC failover, safe_get |
| `leaderboard.py` | 修改 | safe_get, 缓存保鲜 |
| `server.py` | 修改 | HMAC 防重放 |
| `deposit_wallet.py` | 修改 | RPC failover |
| `wallet_selector.py` | 修改 | calc_risk_score 实现 |
| `backtest.py` | 修改 | safe_get, 跳过类型 |
| `common/__init__.py` | **新建** | 包初始化 |
| `common/rate_limit.py` | **新建** | API 限速器 |
| `common/api_monitor.py` | **新建** | API 监控统计 |
| `common/feishu.py` | **新建** | 飞书通知 (含重试) |
| `tests/` | **新建** | pytest 测试套件 |
| `CHANGELOG.md` | **新建** | 本文件 |
| `results/trader_scores.json` | 修改 | VPenguin 测试数据清理 |
| `backup_20260530/` | **新建** | 所有源文件备份 |

---

## 验证结果

- `python3 -m py_compile` 所有 .py 文件 → ✅ 全部通过（仅 1 个 SyntaxWarning，既存问题 `\$`）
- `python3 -m pytest tests/ -v` → ✅ **30/30 通过**
- 所有第三方库导入正常（`config`, `common.*` 模块导入链完整）
- `/etc/poly-bot.env` 权限 600 ✅ 已配置

---

## 任务 JJC-20260530-004 — 实盘启动 + 模拟交易记录器

> 皇上旨意: "我接受这个风险，我可以去赌一把，47美金亏了也没事。然后能不能加一个模拟交易模块，用来记录接下来一个月的实盘数据。"
> 执行日期: 2026-05-30

### 配置变更

| 文件名 | 变更 |
|--------|------|
| `.env` | `MIN_VIABLE_CAPITAL=500→47`, `MAX_DAILY_LOSS=3→10`, `MAX_POSITION_LOSS=2.35→4.00`, `FOLLOW_WALLETS` 精简为仅 VPenguin |
| `/etc/poly-bot.env` | 同上配置同步更新 |
| `/etc/systemd/system/poly-bot.service` | 添加 `--live` 标志 |

### 新增模块

| 文件 | 说明 |
|------|------|
| `sim_recorder.py` | **新建** — 模拟交易记录器，记录每个跟单信号并对 $500/$1000/$2000 资本水平做模拟计算 |

### bot.py 变更

- 添加 `from sim_recorder import record_signal` 导入
- 在跟单复制逻辑中插入信号记录（记录实盘动作 + $500/$1000/$2000 模拟）
- 信号记录在 MIN_TRADE_SIZE 过滤之前，确保所有信号都被捕获

### 模拟记录器功能

- 每天一个 `signals_YYYYMMDD.jsonl` 文件
- 每个记录包含：时间戳、交易员、币种、方向、金额、价格
- 实盘动作（BUY/SKIP）及 $47 资本的跟单金额
- 三个资本水平的模拟：$500/$1000/$2000
- 模拟使用与实盘相同的跟单倍数(0.5x)和比例上限(8%)
- `generate_monthly_report()` 生成 Markdown 月度对比报告
