# Change: 定时对冲累计权益记账

## Why

当前面板只有持仓期未实现盈亏，无法回答实例自统计起点以来的已实现结果。交易所现成累计字段要么不可用，要么是混合其他仓位的账户级口径，因此只能在两腿精确空仓时记录两账户权益。

## What Changes

- Variational 适配器增加从 `/portfolio` 读取余额、未实现盈亏和总权益的能力。
- 定时策略仅在一轮平仓后两腿实仓精确为零时，向独立 JSONL 追加权益快照。
- 权益查询和写盘完全隔离，失败不得改变交易结果或互锁状态。
- 本地只读面板以首末快照的 Decimal 差值展示实例及全部实例累计盈亏。
- 少于两条有效快照时显示“累计中…”，并在可用时标明统计起点和轮数。

## Impact

- 修改 `adapters/variational_client.py`、`timed_volume/strategy.py`、`tools/run_timed_volume.py` 和 `tools/hedge_panel.py`。
- 增加离线测试，不访问交易所，不改变开平仓、收敛、互锁或轮次调度行为。
