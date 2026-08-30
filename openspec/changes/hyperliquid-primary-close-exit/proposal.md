# Change: Hyperliquid 主腿与安全平仓退出

## Why

SNDK 定时对冲需要从 Variational × Hyperliquid 迁移为 Hyperliquid xyz × io，
以便两腿成交量都计入 Hyperliquid builder dex，同时继续共用同一账户和保证金池。

现有循环在平仓成功后会立即进入下一节拍开新轮。人工在 `closed` 后终止进程存在
落在两腿执行之间并制造裸仓的风险，因此需要一个只平当前轮、绝不开新轮的确定性
退出模式。

## What Changes

- `run_timed_volume` 的主腿新增 Hyperliquid 场馆与独立环境变量前缀。
- Hyperliquid 默认加载的 builder dex 增加 `xyz`，使 `xyz:` 前缀标的可解析。
- 新增 `--close-and-exit`：已有轮次立即到期并持续平仓，成功后退出；无进行中
  轮次时不推进状态机。
- 启动摘要显示主腿场馆与环境变量前缀，平仓退出前显示最终双腿持仓与净敞口。
- 明确同一 Hyperliquid 账户在不同市场上的两腿不构成重复占用。

## Impact

- Affected specs: `timed-hedged-volume`、`hyperliquid-adapter`
- Affected code: `tools/run_timed_volume.py`、`adapters/hyperliquid_client.py`
- Affected tests: `tests/test_run_timed_volume.py`、`tests/test_hyperliquid_client.py`
- 默认运行路径保持不变；只有显式选择 Hyperliquid 主腿或平仓退出模式时启用新行为。
