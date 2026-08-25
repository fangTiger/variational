# Change: 定时对冲开仓前基差门控

## Why

定时定量策略当前在空仓节拍直接开仓，异常的两腿入场基差会立即转化为浮亏。
结构性基差不能按绝对值拦截，因此需要以单实例近期入场基差为基线判断相对偏离。

## What Changes

- 新开仓前计算两腿中价基差，并与最近 20 轮入场基差的中位数和标准差比较。
- 样本不足五轮或历史标准差低于 0.02% 时直接放行。
- 超阈值时等待下一节拍；累计等待达到上限后无条件开仓。
- 默认 `sigma=0`，保持所有既有实例交易语义不变。
- 心跳记录偏离、累计等待秒数和 `open` / `waiting` / `forced` 状态。

## Impact

- Affected specs: `timed-hedged-volume`
- Affected code: `timed_volume/strategy.py`、`tools/run_timed_volume.py`
- Affected tests: `tests/test_timed_hedged_volume.py`、`tests/test_run_timed_volume.py`

