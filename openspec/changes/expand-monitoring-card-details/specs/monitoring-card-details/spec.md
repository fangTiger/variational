## ADDED Requirements

### Requirement: 网格卡片展示运行详情
网格 provider MUST 在既有 5 项指标之后追加 9 项指定详情，并按阈值设置 tone。“持仓” MUST 同时显示 `inv_btc` 数量与 `inv_usd` 美元金额；“库存上限” MUST 使用绝对值显示当前美元占用、上限及百分比。熔断余量 MUST 按 `当前权益 - 历史权益峰值 × 0.88` 计算，百分比 MUST 使用该差额除以当前权益。权益峰值文件缺失、损坏或数据不可计算时 MUST 显示 `—` 且不得抛异常。

#### Scenario: 负持仓展示美元占用
- **WHEN** `inv_btc` 为 `-0.03012`、`inv_usd` 为 `-1912`、库存上限为 `3750`
- **THEN** “持仓”显示 `-0.03012 BTC / $-1,912`，“库存上限”显示 `$1,912 / $3,750 (51%)`

#### Scenario: 熔断余量正常
- **WHEN** 当前权益为 997.87 且历史峰值为 1003.417376
- **THEN** 熔断线为峰值的 88%，卡片显示当前权益到该线的美元差额及占当前权益百分比

#### Scenario: 峰值文件不可用
- **WHEN** `equity_peak.json` 缺失、损坏或不含有效峰值
- **THEN** “距熔断线”显示 `—`、tone 为 `normal`，其他网格指标继续显示

### Requirement: 对冲心跳安全透传持仓指标
对冲心跳 MUST 从当轮 `state.primary.raw` 字典的 `position_value` 与 `unrealized_pnl` 分别读取 `primary_notional` 与 `primary_unrealized`，并把 CLI 的 `max_primary_notional` 写入心跳。心跳 MUST 从当轮 `state.hedge.raw` 对象的 `value` 与 `unrealised_pnl` 分别读取 `hedge_notional` 与 `hedge_unrealized`。读取或转换任一原始字段失败时，该字段 MUST 独立降级为 `None` 并继续写心跳。该变更 MUST NOT 发出额外接口请求或修改任何交易、风控、上限门禁和清算逻辑。

#### Scenario: 原始持仓包含名义金额与浮盈
- **WHEN** 当轮 primary 原始字典包含 `position_value`、`unrealized_pnl`，且 hedge 原始对象包含 `value`、`unrealised_pnl`
- **THEN** 心跳按原字段拼写分别写入四个值和 CLI 名义上限

#### Scenario: 原始持仓字段不可用
- **WHEN** 任一持仓为空、原始对象类型异常、字段缺失、取值或字符串转换抛异常
- **THEN** 对应心跳字段写入 `null`，其他可读字段仍写入并继续完成快照

### Requirement: 对冲卡片兼容新旧心跳
对冲 provider MUST 让“Lighter 腿”和“Extended 腿”分别同时显示五位小数的带符号 BTC 数量与对应名义美元金额，并在既有指标之后追加名义金额、上限占用、两腿权益和再平衡阈值。任一腿的数量或名义金额缺失、无效时，该腿 MUST 显示 `—`，不得让整张卡片失败。

#### Scenario: 两腿数量与美元金额完整
- **WHEN** Lighter 数量与名义金额为 `0.02362`、`1499`，Extended 数量与名义金额为 `-0.02362`、`1499`
- **THEN** 两腿分别显示 `+0.02362 BTC / $1,499` 和 `-0.02362 BTC / $1,499`

#### Scenario: 旧心跳缺少 Extended 名义金额
- **WHEN** 心跳没有 `hedge_notional`
- **THEN** “Extended 腿”显示 `—`，卡片其他指标继续显示

#### Scenario: 上限占用阈值
- **WHEN** 名义金额不超过上限、超过 90% 或超过上限
- **THEN** “上限占用”的 tone 分别为 `normal`、`warn` 或 `bad`

### Requirement: 归因卡片标注收益质量
归因 provider MUST 追加权益变化、资金费、闭环年化和最大回撤 4 项指标。正负权益变化 MUST 分别使用 `good` 和 `bad`；最大回撤超过 5% MUST 使用 `warn`；观察期不足 7 天时闭环年化 MUST 追加 `（样本不足）`。

#### Scenario: 短观察期年化
- **WHEN** 观察期少于 7 天
- **THEN** 闭环年化数值后显示 `（样本不足）`

### Requirement: 总览统计系统在线数
注册表 MUST 只把 `alive` 为布尔值的系统计入系统总数，并只把 `alive is True` 的系统计为在线。渲染器 MUST 在总权益和告警徽章之间显示该统计。

#### Scenario: 归因不计入系统数
- **WHEN** 系统列表含两个 `alive` 为布尔值的系统和一个 `alive is None` 的归因卡片
- **THEN** 总览显示 `2 个系统 / 2 个在线`
