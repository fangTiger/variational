# Change: 挂单前校验价格位于市价正确一侧

## Why

Lighter 做市最近 100 笔订单中，**55 笔状态为 `canceled-post-only`**，仅 11 笔
`filled`。这 55 笔**全部是 SELL**，价格区间 64,233.5~64,296.9，而彼时市价已涨到
64,418.7——**卖单价格低于市价**，必然立即撮合，被 post-only 规则拒绝。

根因是引擎在价格单向上行时，仍持续尝试在**市价下方补卖格**。挂单前没有任何
校验确认目标价位于市价的正确一侧，于是：

- 该格挂单必被拒 → 引擎判定「终态 CANCELLED 且无成交」→ **原格重挂** → 再被拒
- 2026-08-19 13:39 缩格距时，1.5 分钟内重挂 42 次并打满 Lighter 速率限制
  （40 requests per 60 second），形态符合条款禁止的 automation exploitation

`adapters/lighter_order.py:26` 把 `CANCELED` / `CANCELED-POST-ONLY` /
`CANCELED-SELF-TRADE` **三种原因压平成同一个 `CANCELLED`**，引擎因此无法区分
「被动撤单（该重挂）」与「post-only 冲突（重挂必再失败）」，这是死循环的放大器。

此为执行实现率仅 **32%** 的直接原因，也是刷量上不去的核心瓶颈。

> 已排除的假设：格距过窄（实测盘口价差仅 $0.1~$1.1，比格距小两个数量级）；
> 订单过期（SDK 默认 `DEFAULT_28_DAY_ORDER_EXPIRY` = 28 天）；自成交
> （55 笔全为 post-only，无 self-trade）。

## What Changes

- 适配器新增盘口读取能力，返回最优买价与最优卖价。
- 引擎每轮**取一次**盘口并缓存复用（不可每单一次，否则超速率限制）。
- 挂单前校验方向合理性：**SELL 目标价须高于最优卖价，BUY 目标价须低于最优买价**，
  不满足则**跳过该格**，不下单、不入重试队列。
- 订单状态映射保留取消原因，引擎对 post-only 冲突不再原格重挂。
- 同格连续被撤达阈值后进入退避，作为不依赖外部数据的兜底防线。
- 盘口读取失败时**降级放行**，保持现有行为。

## Capabilities

### New Capabilities

- `grid-min-spread-guard`: 定义挂单前的市价侧校验、跳过语义、取消原因保留、
  退避策略、盘口缓存与降级行为。

### Modified Capabilities

无。库存上限、趋势感知、TPSL、熔断等风控行为均不改变。

## Impact

- 修改 `grid/grid_engine.py`：每轮盘口缓存、挂单前校验、按取消原因分流、退避。
- 修改 `adapters/lighter_order.py`：状态映射保留取消原因（不再压平）。
- 修改 `adapters/extended_client.py`、`adapters/lighter_client.py`：各加盘口读取方法。
- 新增无网络测试，失败路径优先。
- **不修改**部署 plist、不调整现行策略参数、不改变库存上限语义。
- 两套实盘共用 `GridEngine`，同时生效。Extended 侧同样受益：任何单向行情下
  「在市价错误一侧补格」都会被拦掉，且正常路径零行为变化。
