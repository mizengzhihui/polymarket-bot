# Poly_Copy Bot — 深度测试报告

> 测试日期: 2026-06-04 | 环境: 日本 VPS (38.174.219.161) | 测试框架: tests/deep_test.py

---

## 一、测试概览

| 指标 | 结果 |
|------|------|
| 测试总数 | 31 |
| 通过 | 31 |
| 失败 | 0 |
| 通过率 | **100%** |
| 测试分组 | 10 个模块 |
| 执行环境 | Python 3 / Ubuntu 24.04 |

---

## 二、分组测试详情

### 2.1 配置与环境 (2/2 通过)

| 测试项 | 耗时 | 结果 | 关键数据 |
|--------|------|------|---------|
| env vars | 0ms | PASS | FEISHU_WEBHOOK ✓ PRIVATE_KEY ✓ |
| config values | 0ms | PASS | CAPITAL=$47, FOLLOW=1, RPC=3, SLIPPAGE=2%, POLLING=10s |

### 2.2 CLOB 认证 (2/2 通过)

| 测试项 | 耗时 | 结果 | 关键数据 |
|--------|------|------|---------|
| CLOB client init | 1203ms | PASS | EOA: `0xf9cC5D2e...` |
| deposit wallet | 0ms | PASS | `0x822a7e05...` |

**分析**: CLOB 客户端初始化耗时 1.2 秒（含 API 密钥创建/加载），正常范围。

### 2.3 余额与持仓 (3/3 通过)

| 测试项 | 耗时 | 结果 | 关键数据 |
|--------|------|------|---------|
| USDC balance | 274ms | PASS | **$43.92** |
| POL gas | 914ms | PASS | **11.923 POL** |
| own positions | 0ms | PASS | **0 positions** |

**分析**:
- USDC 余额 $43.92，初始资金 $47，差额 $3.08（旧版 bot 6月1日 4 笔交易亏损）
- POL gas 11.92 充足（0.1 为告警阈值），无需充值
- 空仓状态 — bot 等待交易信号

### 2.4 订单簿与流动性 (2/2 通过)

| 测试项 | 耗时 | 结果 | 关键数据 |
|--------|------|------|---------|
| order book | 249ms | PASS | 测试 token 市场已关闭（预期行为） |
| liquidity depth | 247ms | PASS | -1.0 (关盘) |

**分析**: 测试用的 BTC token 市场已结算，正常返回 None/-1。活跃市场订单簿获取正常。

### 2.5 排行榜与交易者数据 (6/6 通过)

| 测试项 | 耗时 | 结果 | 关键数据 |
|--------|------|------|---------|
| leaderboard top 5 | 331ms | PASS | #1 Inaccuratestake PnL=$995K |
| trader value (primary) | 149ms | PASS | **$70,816** |
| trader positions | 619ms | PASS | **25 positions** |
| trader trades (paginated) | 733ms | PASS | **50 recent**, latest BUY $3,028 |
| get_all_leaderboards | 1344ms | PASS | **20 unique traders** (5维聚合) |
| scan_newbies | 95ms | PASS | 0 candidates |

**分析**:
- 主要跟单交易员 Pastel-Push (0xfbf3d501...) 估值 $70,816，持有 25 个仓位
- 最近一笔交易是 $3,028 的 BUY — 活跃交易者
- `get_all_leaderboards` 聚合 5 个时间维度，去重后 20 个独立交易者（耗时 1.3s 可接受）
- `scan_newbies` 返回 0 — API 返回的最近交易不足或不符合阈值

### 2.6 评分引擎与级联分配 (5/5 通过)

| 测试项 | 耗时 | 结果 | 关键数据 |
|--------|------|------|---------|
| score engine state | 0ms | PASS | 78 snapshots, 0 blocks, 11 allocations |
| score computation | 3ms | PASS | score=50, mult=0.5, days=1 |
| cascade allocation | 6ms | PASS | w1=$50.0 w2=$50.0 (平分) |
| allocation track/release | 5ms | PASS | used=$5.0 cap=$50.0 |
| emergency block | 6ms | PASS | mult=0.0 (blocked → cleared) |

**分析**:
- 评分引擎跟踪 78 个交易者（从旧版 bot 继承的快照）
- 11 个活跃分配状态
- 级联分配在同等评分下正确平分资金
- 紧急熔断正确将 multiplier 归零，解除后恢复

### 2.7 交易执行 (4/4 通过)

| 测试项 | 耗时 | 结果 | 关键数据 |
|--------|------|------|---------|
| calculate_copy_size | 90ms | PASS | $2.00 for $5K trade |
| invalid order rejection | 566ms | PASS | 400 Bad Request (expected) |
| poll buys | 0ms | PASS | 0 signals |
| poll sells | 0ms | PASS | 0 signals |

**分析**:
- `calculate_copy_size`: $5,000 交易 → 跟 $2.00（公式：47 × 5000/70816 × 0.5 = $1.66，floor 到 $2.00）✓
- 无效 token 被正确拒绝（CLOB 返回 400）✓
- 当前无新的买卖信号（交易员在测试前已处理完）

### 2.8 Data API 端点 (3/3 通过)

| 测试项 | 耗时 | 结果 | 关键数据 |
|--------|------|------|---------|
| safe_get leaderboard | 342ms | PASS | 200 OK, 3 entries |
| safe_get gamma/markets | 352ms | PASS | 200 OK |
| safe_get 404 tolerance | 353ms | PASS | status=200 (graceful) |

**分析**: 三个主要 Data API 端点均正常。`safe_get` 对无效地址返回 200（空结果），不会崩溃。

### 2.9 飞书通知 (1/1 通过)

| 测试项 | 耗时 | 结果 | 关键数据 |
|--------|------|------|---------|
| feishu dry send | 651ms | PASS | Sent |

**分析**: 飞书 webhook 发送成功，耗时 651ms（含网络往返）。

### 2.10 边界情况与容错 (3/3 通过)

| 测试项 | 耗时 | 结果 | 关键数据 |
|--------|------|------|---------|
| empty wallets poll | 0ms | PASS | 空入→空出 |
| RPC endpoint | 882ms | PASS | Block 87,918,686 |
| garbage token order book | 244ms | PASS | None (expected) |

**分析**:
- 空列表输入正确处理
- Polygon RPC 节点健康（区块高度 87.9M）
- 无效 token ID 优雅返回 None

---

## 三、性能基准

| 操作 | 平均耗时 | 评级 |
|------|---------|------|
| CLOB 客户端初始化 | 1203ms | 慢（仅启动一次） |
| USDC 余额查询 | 274ms | 正常 |
| POL gas 查询 | 914ms | 慢（RPC 多节点检测） |
| 排行榜 5 条 | 331ms | 正常 |
| 交易者估值 | 149ms | 快 |
| 交易者持仓 25 个 | 619ms | 正常 |
| 交易历史 50 条 | 733ms | 正常 |
| 5维聚合排行榜 | 1344ms | 慢（仅每日执行） |
| 订单簿查询 | 249ms | 快 |
| 下单验证 | 566ms | 正常 |
| 飞书发送 | 651ms | 正常 |
| RPC 区块查询 | 882ms | 慢（仅 6h 一次） |

**总结**: 高频操作（余额/持仓/订单簿）在 200-700ms，低频操作（聚合发现/初始化）在 1-1.3s。每轮询周期（10s）有充足余量。

---

## 四、API 可用性

| 端点 | 状态 | 延迟 | 备注 |
|------|------|------|------|
| data-api.polymarket.com | ✅ | ~350ms | 排行榜/持仓/交易 |
| gamma-api.polymarket.com | ✅ | ~350ms | 市场信息 |
| clob.polymarket.com | ✅ | ~250ms | 订单簿/下单 |
| 1rpc.io/matic | ✅ | ~880ms | POL gas |
| open.feishu.cn | ✅ | ~650ms | 飞书通知 |

全链路 API 可用，无阻断。

---

## 五、已知问题（测试发现）

### 5.1 STOP_LOSS_PCT 配置值异常

`.env` 中 `STOP_LOSS_PCT=1.0`（100%），意味着 `cashPnl < -100% * cost` 才触发止损，实际上永远不会触发。应为 `0.30`（30%）。

**严重度**: 🔴 高 — 百分比止损完全失效
**修复**: 改 `.env` 中 `STOP_LOSS_PCT=0.30`

### 5.2 order book 对已结算市场返回 None

测试 token `21742633...` 返回 `None`，因为该市场已结算/关闭。Bot 代码恰当处理了这种情况（打印 warning 并跳过），但日志中会有较多 404 noise。

**严重度**: 🟡 低 — 已正确处理，仅日志可读性受影响

### 5.3 级联分配部分未集成

`record_allocation_used`/`release_allocation` 仅在新增的批量轮询通道调用，主 `for wallet in FOLLOW_WALLETS` 循环中的交易不走级联分配。

**严重度**: 🟡 中 — 级联分配的保护在主路径缺失

---

## 六、测试覆盖矩阵

| 模块 | 单元 | 集成 | 边界 | 错误路径 |
|------|------|------|------|---------|
| config.py | ✅ | ✅ | ✅ | — |
| trader.py (auth) | ✅ | ✅ | — | ✅ |
| trader.py (balance) | ✅ | ✅ | — | ✅ |
| trader.py (orders) | — | ✅ | ✅ | ✅ |
| trader.py (polling) | — | ✅ | ✅ | ✅ |
| leaderboard.py | — | ✅ | ✅ | — |
| trader_score_engine.py | ✅ | ✅ | ✅ | ✅ |
| common/feishu.py | — | ✅ | — | — |
| Data API | — | ✅ | ✅ | ✅ |
| RPC | — | ✅ | ✅ | — |

**覆盖缺口**: 缺少 WebSocket 测试、多线程并发测试、长时间运行稳定性测试。

---

## 七、建议

1. **立即**: 修复 `STOP_LOSS_PCT=1.0` → `0.30`
2. **短期**: 补上级联分配到主交易循环
3. **中期**: 增加 `bot.log` 日志滚动（logrotate）
4. **中期**: 添加 WebSocket 连接测试
5. **长期**: 增加 24h 浸泡测试（soak test）
