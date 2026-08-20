# Change: 对冲做市互锁与能力补齐

## Why

策略目标已明确为**零方向风险地刷 volume 赚积分**：Lighter 跑网格产生成交量，
Extended 持镜像反向仓抵消方向敞口，闭环利润归零是预期结果。

但 2026-08-20 的排查发现，这套架构有四个阻塞项，其中两个是致命的：

1. **互锁机制从未实现。** 2026-08-17 的设计文档明确要求「做市机器人必须主动
   检查对冲机器人存活，超过 3×interval 未更新则停止开新仓、只允许撤单——
   宁可不赚，不能裸奔」。在 `run_lighter_mm.py` 与 `grid_engine.py` 中搜索
   对冲心跳相关逻辑，**零匹配**。设计文档称「做市在跑、对冲挂了」是最危险的
   组合，而这正是当前的默认状态。

2. **`LighterClient` 缺 `get_order_by_id`。** `hedge_engine.py:428` 直接调用
   该方法且无能力探测，缺失即抛 `NotImplementedError`。已在 2026-08-20 手工
   离场时实测触发。同一路径被 `grid-trading-window` 的计划离场复用，意味着
   Lighter 侧的定时离场会崩溃并留仓过夜。

3. **对冲机器人没有风控自检。** `grid-risk-selfcheck` 的
   `validate_risk_controls()` 只接在两个网格入口，`run_lighter_hedge.py`
   完全没有，对冲腿可以像 8/19 的 Lighter 那样裸奔。

4. **对冲腿账户 `X10_HEDGE` 权益为 $0。** 实测三个 Extended 账户：
   `X10_GRID` $839.73、`X10_HEDGE` **$-0.00**、`X10` $839.73（与 X10_GRID
   同一 vault）。直接启动对冲机器人会让它拿着空账户去对冲。

## What Changes

- 做市引擎新增**对冲存活互锁**：读取对冲心跳，超时则停止开新仓，
  仅允许撤单与缩小敞口的挂单。
- 互锁失效时**限频告警**，并在心跳与状态文件中可见。
- `LighterClient` 新增 `get_order_by_id`。
- `hedge_engine` 对该能力改为探测式调用，缺失时按既有失败路径降级而非抛出。
- `run_lighter_hedge.py` 接入风控完整性自检与启动状态摘要。
- 对冲腿账户由配置指定，允许指向有资金的账户。
- 未配置互锁时行为与现状一致。

## Capabilities

### New Capabilities

- `grid-hedge-interlock`: 定义对冲存活判定、互锁触发后的挂单语义、
  告警与可观测性、未配置时的兼容行为。

### Modified Capabilities

- `lighter-trading-adapter-foundations`: 新增 `get_order_by_id`。
- `maker-first-hedge`: 订单状态读取改为能力探测式，缺失时降级不抛出。

## Impact

- 修改 `grid/grid_engine.py`：互锁判定与挂单门控。
- 修改 `adapters/lighter_client.py`：新增 `get_order_by_id`。
- 修改 `engine/hedge_engine.py`：能力探测与降级。
- 修改 `tools/run_lighter_mm.py`：互锁参数与心跳字段。
- 修改 `tools/run_lighter_hedge.py`：接入风控自检。
- 新增无网络测试，失败路径优先。
- **不修改**任何 plist；启用由人工在验收后配置。
- **不改动**任何风控阈值与算法，不改变网格策略行为。
- 两个网格当前均已停机，本变更不影响运行中的系统。

## 已知但不在本变更解决

实测对冲的 maker 成交率仅 **10.8%**（15 秒超时，958 样本 25 分钟），
因为对冲方向天然逆势、挂单永远在价格跑离的一侧。按当前刷量规模估算，
Extended taker 成本约**年化 100%**。用户已知悉并明确要求先把功能做好，
成本与规模问题另行决策。本变更不试图优化该成本。
