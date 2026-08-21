# dust-position-risk-handling

## ADDED Requirements

### Requirement: 适配器公开市场最小下单量

交易适配器 SHALL 提供按市场查询最小下单量的异步接口，返回与持仓
`signed_size` 相同的基础资产数量口径；实现 SHALL 使用交易所市场元数据，
不得硬编码市场数值。

#### Scenario: Extended 返回元数据最小量
- **WHEN** 引擎查询 Extended 市场的最小下单量
- **THEN** 适配器返回该市场 `trading_config.min_order_size` 的 Decimal 值

#### Scenario: Lighter 将整数单位换算为基础资产数量
- **WHEN** Lighter 市场元数据给出 `min_base_units=20` 且 `size_decimals=5`
- **THEN** 适配器返回 `0.00020`，并复用 `_load_market_meta` 的市场元数据缓存

### Requirement: 引擎缓存市场最小下单量

网格引擎 SHALL 按市场缓存已验证为有限正数的最小下单量，不得每轮重新请求
交易所。查询失败或返回无效值时 SHALL 短时缓存失败状态，避免同一轮或紧邻轮次
重复请求；缓存过期后 MAY 重试，以允许瞬时故障恢复。

#### Scenario: 同一引擎重复判断只查询一次
- **WHEN** 同一市场在硬止损与 TPSL 维护中重复需要最小下单量
- **THEN** 适配器最小量查询只调用一次

#### Scenario: 查询结果无效
- **WHEN** 适配器返回零、负数、NaN 或无限值
- **THEN** 引擎按查询失败处理，不把该值用于跳过风控

### Requirement: 微仓跳过整仓 TPSL 且视为成功

当整仓 TPSL 已启用、持仓非零，并且绝对持仓量严格低于已知市场最小下单量时，系统 SHALL
让 `_maintain_tpsl` 不提交 TPSL，下游 SHALL 把本次维护视为成功并继续做市
挂单；引擎 SHALL 以限频中文 INFO 或 DEBUG 日志说明“持仓低于最小下单量，
不挂 TPSL”。

#### Scenario: 三单位仓位低于二十单位门槛
- **WHEN** Lighter BTC 持仓为 `0.00003` 且市场最小下单量为 `0.00020`
- **THEN** 不调用 TPSL 下单，不触发“禁止新增风险仓”，网格继续挂单

#### Scenario: 持仓恰好达到门槛
- **WHEN** 绝对持仓量等于市场最小下单量
- **THEN** 继续执行本变更前完整 TPSL 计算、验证、下单与失败处理

#### Scenario: 持仓超过门槛
- **WHEN** 绝对持仓量大于市场最小下单量
- **THEN** 继续执行本变更前完整 TPSL 计算、验证、下单与失败处理

### Requirement: 微仓在硬止损中按零处理

当绝对持仓量严格低于已知市场最小下单量时，`_check_hard_stop` SHALL 按零仓
返回未触发，不查询该仓位的清算信息，也不因缺少清算价进入 fail-safe。

#### Scenario: 微仓没有清算信息
- **WHEN** 微仓低于最小下单量且没有可用清算价
- **THEN** 硬止损返回未触发，不记录 fail-safe ERROR

#### Scenario: 可交易仓位没有清算信息
- **WHEN** 持仓达到或超过最小下单量且没有可用清算价
- **THEN** 完全保留本变更前的 fail-safe 行为

### Requirement: 最小下单量未知时失败关闭

引擎 SHALL 仅在取得有效市场最小下单量后把持仓识别为微仓。查询方法不存在、
查询抛错或返回无效值时 SHALL 保守地把非零持仓视为需要 TPSL 与硬止损检查，
不得静默跳过风控。

#### Scenario: 查询失败仍尝试 TPSL
- **WHEN** 非零持仓的最小下单量查询失败
- **THEN** `_maintain_tpsl` 继续既有 TPSL 流程，成功或失败结果仍按原逻辑传播

#### Scenario: 查询失败仍检查硬止损
- **WHEN** 非零持仓的最小下单量查询失败
- **THEN** `_check_hard_stop` 继续既有清算距离与 fail-safe 流程
