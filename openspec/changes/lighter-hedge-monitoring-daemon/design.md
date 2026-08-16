## Context

`HedgeEngine.run_forever()` 已支持每轮 `on_snapshot(HedgeState)` 回调，`tools.alert_check` 已由 launchd 每 900 秒运行并负责 macOS 通知。连续轮次判定不能依赖进程内计数，因为机器人崩溃后计数会丢失；告警器也不应再次访问 Lighter 或 Extended，否则交易所故障可能同时让机器人和告警检查卡住。

## Goals / Non-Goals

**Goals:**

- 每轮持久化足以独立判断五类告警的结构化事实。
- 缺文件、坏行和部分字段缺失时偏向报警或安全跳过，绝不让告警检查崩溃。
- 连续偏离只在最近两轮均可读且超阈值时触发；单轮中间态保持静默。
- launchd 配置可审查、可由人工安装，但本次不触碰系统 LaunchAgents。

**Non-Goals:**

- 不自动修改 RH Wallet 仓位，不新增自动保证金处置。
- 不从 `alert_check` 请求任何交易所 API。
- 不执行 launchctl、不负责 Task 5 的 24 小时观察和资金结算验证。

## Decisions

1. 使用追加式 JSONL 而非覆盖式 JSON。连续 2/3 轮的事实跨进程保存，机器人崩溃时最后记录仍可审计。
2. 每条记录显式写 `primary_read_ok`、`hedge_read_ok` 和 `primary_notional_exceeded`；告警不解析自然语言动作，避免日志文案变化造成漏报。
3. 净敞口比例按 `abs(net_delta) / max(abs(primary_size), abs(hedge_size))` 计算。两腿标的是同一 BTC，价格因子相消；取较大单腿还能覆盖 primary 已平而 hedge 遗留的风险。
4. 保证金阈值独立于引擎自动降险配置。CLI 默认监控阈值 20%，快照回调查询 `get_free_margin_ratio()`；查询失败仍写心跳，并记录错误，避免一次保证金 API 故障伪装成整个机器人死亡。
5. 告警器保留最近 30 分钟事件，覆盖既有 900 秒调度的两个周期。严重异常即使随后恢复，也会通知一次；6 小时冷却负责抑制重复通知。
6. 所有参与比较的数字必须是有限数，且快照关键字段必须完整。格式变化、`NaN` 或 `Infinity` 产生独立的快照无效告警，不能让五类核心判据 fail-open。
7. 只有 `osascript` 返回 0 才记录告警冷却。通知异常或非零退出时本轮仍以失败退出，但不写冷却，留给下一个 launchd 周期重试。

## Risks / Trade-offs

- [alert-check 每 900 秒调度，告警发现延迟可能大于 90 秒] → 心跳判据严格使用 `3 × interval`；实际通知延迟由既有 launchd 调度上限决定，本次不改其调度。
- [保证金查询增加一条每轮 API 请求] → 仅请求只读账户余额；失败被隔离且仍落心跳。
- [默认 20% 可能偏保守] → 宁可多报不可漏报，同时开放 CLI 参数并在快照中保存实际阈值。
- [风险已恢复后仍可能收到一次通知] → 这是覆盖 900 秒调度间隙的有意取舍，符合宁可多报不可漏报。

## Migration Plan

1. 先用无网络测试验证每种异常状态确实产生对应 Alert key。
2. 人工检查并复制仓库内 plist 到 `~/Library/LaunchAgents/`。
3. 启动后先确认 `data/lighter_hedge.jsonl` 持续追加，再手工执行 `tools.alert_check --dry-run`。
4. 回滚只需由人工停止/卸载新服务；仓库代码不会操作现有网格服务。

## Open Questions

无。阈值默认值与常驻参数由本变更明确记录，可在人工安装前调整 plist。
