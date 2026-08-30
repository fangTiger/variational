# Change: 新增基差信号驱动入场模式

## Why

现有定时定量策略按固定节拍机械交替方向，无法利用两腿基差偏离与回归关系。
离线样本外结果显示，按滚动中位数与总体标准差识别偏离方向，并在基差越过中位线时
退出，能显著改善单轮方向正确率；同时仍需用定时兜底维持最低成交量。

## What Changes

- 新增默认关闭的 `signal` 入场模式；默认 `timer` 完整保留既有行为。
- 从 Hyperliquid `candleSnapshot` 拉取两腿 5 分钟历史 K 线，按时间戳对齐后计算
  基差中位数与总体标准差，并按配置间隔刷新。
- 信号模式按偏离正负决定主腿方向，不再机械交替；样本不足时拒绝开仓。
- 信号仓在基差越过中位线或达到最长持仓时间时平仓。
- 无信号达到配置时长后按定时方向兜底开一轮；零值关闭兜底。
- 心跳增加信号统计、状态与拒绝原因；轮次台账增加入场统计和退出原因。
- `--close-and-exit`、连续平仓失败退避、认证互锁与自愈保持优先级不变。

## Impact

- Affected specs: `timed-hedged-volume`
- Affected code: `timed_volume/strategy.py`、`tools/run_timed_volume.py`
- Affected tests: `tests/test_timed_hedged_volume.py`、`tests/test_run_timed_volume.py`

