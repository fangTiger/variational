## ADDED Requirements

### Requirement: 每轮持久化对冲快照
系统 MUST 在 Lighter 对冲引擎每轮结束后向 `data/lighter_hedge.jsonl` 追加一条 JSON 记录。记录 MUST 包含时间戳、轮询间隔、两腿有符号仓位、净敞口、本轮动作、两腿读取状态、primary 名义超限状态、Extended 可用保证金率及其告警阈值。保证金读取失败 MUST NOT 阻止心跳写入。

#### Scenario: 正常一轮写出快照
- **WHEN** 两腿读取成功且本轮完成再平衡判断
- **THEN** JSONL 新增一条同时包含两腿仓位、净敞口和动作的记录

#### Scenario: primary 读取失败仍写心跳
- **WHEN** 本轮无法读取 primary 持仓
- **THEN** 快照记录 `primary_read_ok=false` 且仍包含当前时间戳

#### Scenario: 保证金查询失败仍写心跳
- **WHEN** Extended 保证金率查询抛出异常
- **THEN** 快照保证金率为空、记录错误信息且仍成功追加

### Requirement: 心跳缺失告警
告警系统 MUST 在快照缺失或最后有效记录超过该记录轮询间隔的三倍未更新时产生告警。

#### Scenario: 默认 90 秒没有快照
- **WHEN** 最近快照的 interval 为 30 秒且 age 大于 90 秒
- **THEN** 告警集合包含 Lighter 对冲心跳陈旧告警

### Requirement: 无效快照不得静默绕过告警
告警系统 MUST 拒绝关键字段缺失以及 `NaN`、`Infinity` 等非有限数值。无效快照 MUST 产生独立告警；无效时间戳或轮询间隔还 MUST 按心跳陈旧处理。

#### Scenario: API 或快照格式变化
- **WHEN** 最新快照有时间戳但缺少读取状态、仓位、阈值或保证金字段
- **THEN** 告警集合包含快照无效告警

#### Scenario: 非有限时间绕过比较
- **WHEN** 最新快照的时间戳为 `NaN` 或 interval 为 `Infinity`
- **THEN** 告警集合同时包含快照无效和心跳陈旧告警

#### Scenario: 远未来时间绕过比较
- **WHEN** 最新快照时间戳超前当前时间超过 60 秒
- **THEN** 告警集合同时包含快照无效和心跳陈旧告警

### Requirement: 持续净敞口告警
告警系统 MUST 仅在连续两轮均成功读取两腿，且 `abs(net_delta)` 相对较大单腿绝对仓位的比例均严格超过再平衡阈值时告警。事件 MUST 在最近 30 分钟内保留，以覆盖 900 秒告警调度间隙。

#### Scenario: 连续两轮偏离
- **WHEN** 最近两轮净敞口比例都大于各自阈值
- **THEN** 告警集合包含持续净敞口告警

#### Scenario: 只有一轮偏离
- **WHEN** 前一轮已收敛而最新一轮偏离
- **THEN** 不产生持续净敞口告警

### Requirement: primary 名义超限立即告警
系统 MUST 在最近 30 分钟内任一轮结构化字段表明 `max_primary_notional` 门禁触发时告警，避免事件被下一次调度前的健康快照覆盖。

#### Scenario: 门禁触发
- **WHEN** 最新快照的 `primary_notional_exceeded` 为真
- **THEN** 告警集合包含要求人工缩仓的名义超限告警

### Requirement: primary 连续读取失败告警
系统 MUST 在最近 30 分钟内出现连续三轮 `primary_read_ok=false` 时告警；一轮成功读取 MUST 重置当前连续序列，但不得抹掉等待通知的既有三连失败事件。

#### Scenario: 连续三轮失败
- **WHEN** 最近三条有效快照都无法读取 primary
- **THEN** 告警集合包含 primary 连续读取失败告警

### Requirement: 对冲腿保证金告警
系统 MUST 在最近 30 分钟内任一快照的 Extended 可用保证金率严格低于记录中的阈值时告警。

#### Scenario: 可用保证金不足
- **WHEN** 最新快照记录可用保证金率 15%、阈值 20%
- **THEN** 告警集合包含 Extended 保证金不足告警

### Requirement: 通知失败不得进入冷却
系统 MUST 仅在 `osascript` 成功退出后记录该告警的冷却时间。通知抛出异常或非零退出时 MUST 保持无冷却状态，供下一调度周期重试。

#### Scenario: macOS 通知发送失败
- **WHEN** `osascript` 返回非零退出码
- **THEN** 告警检查以有异常状态退出，且不保存该告警的 6 小时冷却

#### Scenario: 冷却状态损坏或来自未来
- **WHEN** 冷却文件不是 JSON 对象，或某告警时间为非法值、非有限数或未来时间
- **THEN** 系统忽略该冷却值并尝试发送当前告警

### Requirement: 提供 launchd 常驻配置
仓库 MUST 包含 Label 为 `com.variational.lighter-hedge` 的 plist，使用项目虚拟环境和 `tools.run_lighter_hedge --live`，设置 `KeepAlive=true`、`ThrottleInterval=30`、项目 WorkingDirectory/PYTHONPATH 和独立 stdout/stderr 日志。

#### Scenario: 人工安装前校验 plist
- **WHEN** 对仓库内 plist 执行 plist 语法检查
- **THEN** 配置合法且没有修改用户 LaunchAgents 目录
