# Polymarket 跟单Bot

自动化跟单机器人，监控 Polymarket 预测市场中的顶级交易员，自动复制其交易。

## 项目状态

✅ **v1.3 已上线运行** — 2026-06-01

| 组件 | 状态 |
|------|------|
| 排行榜发现 | ✅ 39名候选人 / 24h更新 |
| 统一评分 | ✅ 运行中 |
| 瀑布分配 | ✅ 运行中 |
| 轮询监测 (20s) | ✅ 运行中 |
| 止损 (-50%) | ✅ 运行中 |
| 平仓跟单 | ✅ 运行中 |

## 策略概要

### 整体流程

发现交易员 → 统一评分 → 瀑布分配 → 执行跟单

### 数据方案

**Data API 轮询（主）**：每 20 秒轮询跟单对象的买入交易
**Data API 轮询（辅）**：每 30 秒检查止损/平仓信号

> WebSocket：Polymarket 的 WebSocket 市场频道不支持按钱包地址筛选，仅作为可选的价格监控增强。

### 评分公式

```
跟单评分 = 胜率 x 平均ROI x log(交易次数 + 1) x 置信度系数
```

- **置信度系数** = 1 - 1/(样本量 + 1)，样本越多越可信
- **连续止损折扣**：每止损一次评分 x 0.8（24h 后重置）

### 资金分配：瀑布分配法

按评分权重设定每人最高上限。24 小时未开单的份额自动释放给他人使用。可用资金不足时放弃跟单，等待释放。

### 风控

- **止损**: 单笔浮亏 50% 时通过 CLOB 卖出平仓
- **滑点容忍度**: 2%，超出则放弃
- **总仓位**: 不设上限，由瀑布分配法约束

## 文件结构

```
├── bot.py                    # 主循环 (轮询 20s/30s)
├── config.py                 # 配置文件
├── leaderboard.py            # 交易员发现
├── trader.py                 # CLOB 下单 + API 交互
├── trader_score_engine.py    # 评分引擎 + 瀑布分配
├── trader_monitor.py         # 行为监测
├── wallet_selector.py        # 钱包管理
├── server.py                 # Web 面板
├── backtest.py               # 回测框架
├── common/                   # 通用模块
├── deploy/                   # systemd 部署
├── results/                  # 运行数据 (自动生成)
└── .env                      # 环境变量
```

## 快速开始

### 安装

```bash
pip install -r requirements.txt
```

### 配置 `.env`

```
POLYMARKET_PRIVATE_KEY=你的私钥
USER_CAPITAL=50
MIN_VIABLE_CAPITAL=20
FEISHU_WEBHOOK=https://open.feishu.cn/open-apis/bot/v2/hook/你的webhook
```

### 运行

```bash
python3 bot.py
```

###  systemd 部署

```bash
cp deploy/poly-bot.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable poly-bot
systemctl start poly-bot
```

## 参数说明

| 参数 | 默认 | 说明 |
|------|------|------|
| `USER_CAPITAL` | 50 | 初始资金池 ($) |
| `STOP_LOSS_PCT` | 0.50 | 止损 (-50%) |
| `TAKE_PROFIT_PCT` | 0.50 | 止盈 (+50%) |
| `SLIPPAGE_TOLERANCE` | 0.02 | 滑点容忍度 (2%) |
| `SCORE_UPDATE_INTERVAL_HOURS` | 24 | 评分更新周期 |
| `POLLING_INTERVAL_SEC` | 20 | 买入轮询间隔 (秒) |
| `CLOSE_MONITOR_INTERVAL_SEC` | 30 | 平仓监测间隔 (秒) |
| `INITIAL_FOLLOW_COUNT` | 5 | 初始跟单人数 |
| `ALLOCATION_TIMEOUT_HOURS` | 24 | 未开单释放时间 |
| `ALLOCATION_PAUSE_THRESHOLD` | 0.80 | 资金用满 80% 暂停 |

## 深度测试

两轮深度测试覆盖：

- **代码审计**: 语法检查 / 模块导入 / 逻辑追踪
- **功能性测试**: Gas 查询 / 钱包余额 / 订单簿 / 跟单对象数据 / 下单 / 平仓
- **边界条件**: 重启恢复 / 资金超分 / 空 order_id / Key 名不匹配

所有关键问题均已修复并验证通过。

## 版本历史

| 版本 | 说明 |
|------|------|
| `v1.0` | 原始版本，硬编码跟单对象，纯轮询 |
| `v1.3` | 排行榜发现 + 统一评分 + 瀑布分配 + 20s 轮询 |
