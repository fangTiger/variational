## ADDED Requirements

### Requirement: 可选 primary 名义金额上限
系统 MUST 支持默认为 `None` 的 `max_primary_notional` 配置；启用时，仅当 `abs(primary_size) * mark_price` 严格大于上限才阻止再平衡。系统 MUST 在该门禁前运行清算保护和保证金降险，名义上限 MUST NOT 阻止降低风险的动作或吞掉相关告警。

#### Scenario: 超过上限
- **WHEN** primary 名义金额严格大于配置上限且没有更早触发的风险保护
- **THEN** 本轮动作包含明确超限说明且不调用再平衡的 `market_order`

#### Scenario: 超限时已有对冲仓
- **WHEN** primary 超限且 hedge 已持有对冲仓
- **THEN** 系统保持已有对冲仓不动，不平仓也不再平衡

#### Scenario: 恰好等于上限
- **WHEN** primary 名义金额恰好等于配置上限
- **THEN** 系统允许进入现有再平衡流程

#### Scenario: 未配置上限
- **WHEN** `max_primary_notional=None`
- **THEN** 系统保持现有再平衡行为

#### Scenario: 超限且只读 primary 触发风险保护
- **WHEN** primary 超限、声明只读，且清算接近或低保证金保护被触发
- **THEN** 系统先返回对应人工风险告警，不调用任一腿的 `market_order`

### Requirement: 只读 primary 不得触发单腿改仓
系统 MUST 允许适配器声明不支持自动交易；当 primary 只读时，任何需要同时改变两腿仓位的清算保护或保证金降险 MUST 转为人工告警并保持两腿不动。

#### Scenario: 只读 primary 逼近清算
- **WHEN** primary 声明只读且任一腿触发清算接近保护
- **THEN** 系统提示人工处理且不调用任一腿的 `market_order`

#### Scenario: 只读 primary 遇到低保证金
- **WHEN** primary 声明只读且 hedge 可用保证金率低于阈值
- **THEN** 系统提示人工处理且不调用任一腿的 `market_order`

### Requirement: 可配置的自愈异常类型
系统 MUST 仅通过配置的异常类型集合识别需要交给 `run_forever` 自愈的 primary 读取异常，默认集合 MUST 为空，并 MUST 保留现有自愈回调流程。空配置 MUST 表示没有异常需要自愈，系统 MUST NOT 通过异常类名或其后缀猜测异常语义。

#### Scenario: 配置的异常触发自愈路径
- **WHEN** primary 抛出配置集合中的异常类型
- **THEN** `run_once` 向上抛出异常，`run_forever` 调用既有自愈流程

#### Scenario: 普通读取异常
- **WHEN** primary 抛出不在配置集合中的异常
- **THEN** 系统跳过本轮并保持两腿仓位不动

#### Scenario: 空配置不猜测认证异常
- **WHEN** 异常类型集合为空且 primary 抛出的异常类名以 `AuthError` 结尾
- **THEN** 系统将其作为普通读取异常处理，不触发自愈流程
