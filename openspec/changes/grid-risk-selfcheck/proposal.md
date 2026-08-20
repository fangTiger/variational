# Change: 风控完整性自检与启动摘要

## Why

2026-08-19 BTC 单日上涨 **8.5%**（64,086 → 69,528），两个网格结局完全不同：

- **Extended**（传了 `--trend-aware`）：价格破 band 上沿后冻结、reduce-only 减仓
  阶梯把空头买回到 0，主动止损离场，权益自峰值 −$108（−10.7%）。
- **Lighter**（三个风控 flag 一个都没传）：无 band 概念，一路加空到
  **−$3,943（占上限 105%）**，满仓硬扛全部涨幅。

根因**不在代码，在配置与能力探测**：

1. `--trend-aware` / `--max-drawdown` / `--hard-stop-dist` 都是可选参数，
   `deploy/com.variational.lighter-mm.plist` 三个全无，而**启动日志完全不打印
   风控状态**，因此裸奔了整整一天无人察觉。
2. `LighterClient` 只有 `get_collateral()`、没有 `get_balance()`，而引擎两处
   风控用 `getattr(self.ext,"get_balance",None)` 探测，拿不到就**静默跳过**：
   - `_check_equity_drawdown`：净值回撤熔断直接 `return False`，从未运行过
     （佐证：`data/lighter_mm/` 下从来没有 `equity_peak.json`）
   - `_maintain_tpsl`：失去「权益 10% 亏损价」约束，只剩清算缓冲价，而强平在
     +79.6% 之外，TPSL 形同虚设

**一个缺失的方法同时废掉两层风控，且全程无任何告警。**

## What Changes

- 引擎启动时执行**风控完整性自检**：逐层校验每个风控所依赖的适配器能力。
- 关键风控依赖缺失时**拒绝启动**并给出中文原因，不再静默降级裸奔。
- 允许显式声明「知情放弃」某层风控，此时以 WARNING 放行而非拒绝。
- 启动日志新增**风控状态摘要行**，逐层列出启用/关闭/不可用。
- 运行期风控因能力缺失而跳过时，改为**限频告警**，不再完全静默。
- `LighterClient` 新增 `get_balance()`，解锁净值熔断与 TPSL 权益约束。

## Capabilities

### New Capabilities

- `grid-risk-selfcheck`: 定义风控层与其依赖能力的映射、启动自检与拒绝启动条件、
  知情放弃声明、启动摘要格式、运行期缺失告警。

### Modified Capabilities

- `lighter-trading-adapter-foundations`: 新增 `get_balance()`，返回引擎可识别的
  权益视图。

## Impact

- 修改 `grid/grid_engine.py`：自检、启动摘要、运行期缺失告警。
- 修改 `adapters/lighter_client.py`：新增 `get_balance()`。
- 修改 `tools/run_grid.py`、`tools/run_lighter_mm.py`：知情放弃开关与摘要输出。
- 新增无网络测试，失败路径优先。
- **不修改**任何 plist（补 flag 由人工在自检通过后单独执行）、不改动
  现行策略参数、不改变任何风控的触发阈值与算法。
- 两个网格当前均已停机，本变更不影响运行中的系统。
