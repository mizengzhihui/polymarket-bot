# Polymarket 跟单Bot

自动化跟单机器人，监控 Polymarket 预测市场中的顶级交易员，自动复制其交易。

## 策略概要

### 整体流程

1. **发现交易员** — 从日榜、周榜、月榜、年榜、全部五个排行榜自动获取 Top20 交易员，去重合并
2. **新秀监控** — 扫描 7 天内交易 ≥ 3 笔、胜率 ≥ 70%、收益 ≥ $10 的未上榜地址
3. **统一评分** — 使用单一评分公式对所有候选人打分排序
4. **瀑布分配** — 按评分权重动态分配资金，24小时未开单的份额释放给他人
5. **执行跟单** — WebSocket 实时监听 + HTTP 轮询兜底，通过 CLOB API 执行

### 评分公式

```
跟单评分 = 胜率 × 平均ROI × log(交易次数 + 1) × 置信度系数
```

- **胜率**: 交易赢率 (0~1)
- **平均ROI**: 单笔平均回报率
- **活跃度**: 交易次数经对数压缩
- **置信度系数**: = 1 - 1/(样本量 + 1)，样本越多越可信

### 资金分配: 瀑布分配法

| 场景 | 操作 |
|------|------|
| 某人评分占 50% | 最高分配上限为总资金的 50% |
| 24 小时未开单 | 份额暂时释放给他人使用 |
| 可用资金不足 | 放弃该笔，等有人平仓释放 |
| 已分配 ≥ 80% | 暂停开新单 |

### 风控

- **止损**: 单笔浮亏 50% 时通过 CLOB 卖出平仓
- **连续止损保护**: 每止损一次评分 ×0.8
- **滑点容忍度**: 2%，超出则放弃

## 文件结构

```
├── bot.py                    # 主循环 (WebSocket + 轮询双轨)
├── config.py                 # 配置文件 (所有可调参数)
├── leaderboard.py            # 交易员发现 (排行榜 + 新秀监控)
├── trader.py                 # CLOB 下单 + WebSocket + 平仓轮询
├── trader_score_engine.py    # 评分引擎 + 瀑布分配
├── trader_monitor.py         # 行为监测 (风格漂移检测)
├── wallet_selector.py        # 钱包管理
├── server.py                 # Web 面板
├── backtest.py               # 回测框架
├── common/                   # 通用模块 (飞书通知、限流、API监控)
├── deploy/                   # systemd 部署文件
├── results/                  # 运行数据 (自动生成)
└── .env                      # 环境变量 (私钥、配置)
```

## 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

### 2. 配置环境变量

创建 `.env` 文件:

```
POLYMARKET_PRIVATE_KEY=0x你的私钥
USER_CAPITAL=50
MIN_VIABLE_CAPITAL=20
FEISHU_WEBHOOK=https://open.feishu.cn/open-apis/bot/v2/hook/你的webhook
```

### 3. 运行

```bash
python3 bot.py
```

### 4. systemd 部署

```bash
cp deploy/poly-bot.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable poly-bot
systemctl start poly-bot
```

## 参数说明

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `USER_CAPITAL` | 50 | 初始资金池 ($) |
| `STOP_LOSS_PCT` | 0.50 | 单笔止损 (-50%) |
| `SLIPPAGE_TOLERANCE` | 0.02 | 滑点容忍度 (2%) |
| `SCORE_UPDATE_INTERVAL_HOURS` | 24 | 评分更新周期 (小时) |
| `INITIAL_FOLLOW_COUNT` | 5 | 初始跟单人数 |
| `POLLING_INTERVAL_SEC` | 300 | 轮询间隔 (秒) |

完整参数见 `config.py`。

## 版本

- `main` — 原始版本 (稳定运行中)
- `v1.3-refactor` — 最新策略 (WebSocket + 瀑布分配 + 统一评分)

## 路线图

- [x] 排行榜自动发现交易员
- [x] 新秀监控
- [x] 统一评分体系
- [x] WebSocket 实时监听
- [x] 瀑布分配法
- [x] 连续止损保护
- [ ] 实盘回测
- [ ] 监控告警
