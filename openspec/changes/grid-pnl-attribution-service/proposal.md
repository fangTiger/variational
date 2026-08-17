## Why

网格收益归因 Task 1–5 已具备成交持久化、离线配对、SQLite 存储和纯函数归因能力，但还没有可定时执行的主流程，Task 5 的恒等式残差也没有接入真实账户未实现盈亏。当前已有持续运行的真实成交数据，需要在不干扰实盘进程的前提下生成可审计报告并主动暴露残差与 4 周判据。

## What Changes

- 新增 `tools.pnl_attribution`，使用网格账户只读凭据导入成交、权益和资金费，离线配对后写入 SQLite 与 `data/attribution.json`。
- 观察窗口从已有归因起点或最早引擎运行标识开始；闭环、资金费和权益均按同一窗口计算。
- 从 `X10_GRID` 账户的同一次余额快照读取结束权益与未实现盈亏，显式调用 Task 5 恒等式；缺少历史起点未实现盈亏时在报告中保留假设说明，不伪造零残差。
- `tools.alert_check` 接入残差超阈值和满 4 周判据不通过两类告警，保留现有网格与 Lighter 告警。
- 仓库 `deploy/` 新增每小时一次的 launchd plist，仅供人工安装；更新 README 的服务清单和查看方式。

## Capabilities

### New Capabilities

- `grid-pnl-attribution-service`: 定义只读归因主流程、恒等式结果、主动告警、每小时 plist 和运维文档。

## Impact

- 新增 `tools/pnl_attribution.py`、归因 CLI 测试和 `deploy/com.variational.pnl-attribution.plist`。
- 修改 `tools/alert_check.py`、对应告警测试和 `README.md`。
- 不修改 Task 1–5 的配对或归因纯函数，不进入 Task 7–9，不访问测试网络，不安装 plist，不执行 `launchctl`，不触碰实盘进程或账户交易状态。
