## Why

Lighter 对冲机器人即将无人值守常驻运行，但当前没有独立心跳，也无法从外部识别持续净敞口、人工 primary 名义超限、连续读取失败和 Extended 保证金不足。仓库曾发生网格无声停摆 35.5 小时，因此常驻上线前必须让既有 macOS 通知链路覆盖这些失败状态。

## What Changes

- Lighter 对冲机器人每轮向 `data/lighter_hedge.jsonl` 追加结构化快照，包含时间戳、两腿仓位、净敞口、本轮动作、读取状态、名义超限事件和保证金率。
- `tools.alert_check` 从最近有效快照判断心跳陈旧、连续两轮净敞口偏离、名义上限触发、连续三轮 primary 读取失败和对冲腿可用保证金率过低。
- 保证金告警阈值可通过 CLI 配置，默认 20%，并随快照记录，告警器不访问交易所网络。
- 新增仓库内 launchd plist，让 Lighter 对冲进程 KeepAlive 常驻并写独立标准输出/错误日志。
- 更新 README 的服务清单、前台启动和人工安装说明。

## Capabilities

### New Capabilities

- `lighter-hedge-monitoring`: 定义 Lighter 对冲快照格式、五类主动告警和 launchd 常驻配置。

### Modified Capabilities

- `lighter-hedge-cli`: 新增保证金告警阈值参数，并为引擎装配每轮快照回调。

## Impact

- 修改 `tools/run_lighter_hedge.py`、`tools/alert_check.py`、README 和对应测试。
- 修改 `engine/hedge_engine.py`，以结构化字段暴露名义上限是否触发。
- 新增 `deploy/com.variational.lighter-hedge.plist`。
- 不访问真实网络，不修改 `.env`、网格实现或用户的 `~/Library/LaunchAgents/`。
