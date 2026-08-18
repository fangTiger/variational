## ADDED Requirements

### Requirement: 做市心跳携带动态告警字段
系统 MUST 在每条 Lighter 做市心跳中写入命令行快轮询秒数 `interval`，并在本轮
同时取得持仓数量与标记价时写入二者乘积 `inventory_usd`。标记价不可用时
`inventory_usd` MUST 为 `null`，不得编造零值或其他估值。

#### Scenario: 成功轮次取得持仓和价格
- **WHEN** 做市轮次读取到持仓数量与标记价
- **THEN** 心跳包含实际 `interval` 和带符号的库存名义额

#### Scenario: 轮次没有可用价格
- **WHEN** 做市轮次结束时没有取得标记价
- **THEN** 心跳仍被写入且 `inventory_usd` 为 `null`

### Requirement: 未部署的做市机器人保持告警沉默
系统 MUST 在 `data/lighter_mm.jsonl` 不存在时不产生任何 `lighter_mm_` 告警；文件
存在但没有有效 JSON 对象时 MUST 产生做市心跳缺失告警。

#### Scenario: 心跳文件不存在
- **WHEN** 告警检查运行且做市心跳文件不存在
- **THEN** 做市告警收集返回空列表

#### Scenario: 已创建文件没有有效记录
- **WHEN** 心跳文件存在但为空或只有损坏行
- **THEN** 产生以 `lighter_mm_` 开头的心跳缺失告警

### Requirement: 按心跳间隔识别做市停摆
系统 MUST 以最新心跳的三倍正数 `interval` 作为陈旧门槛；字段缺失、非有限或
非正时 MUST 回落到 5 秒。超过门槛或时间戳超前当前时间超过既有容差时 MUST
产生「做市心跳陈旧」告警，文案 MUST 包含未更新时间和门槛。

#### Scenario: 心跳超过三轮没有更新
- **WHEN** 最新心跳年龄大于 `3 × interval`
- **THEN** 产生做市心跳陈旧告警并显示年龄与门槛

#### Scenario: 远未来时间戳
- **WHEN** 最新时间戳超前当前时间超过容差
- **THEN** 不把该记录视为新鲜并产生做市心跳陈旧告警

### Requirement: 识别做市连续失败
系统 MUST 在至少连续三条心跳的 `success` 明确为 `false` 时产生做市连续失败
告警，并在文案中写明连续失败轮数。所有告警 key MUST 使用 `lighter_mm_` 前缀。

#### Scenario: 连续三轮失败
- **WHEN** 最近连续三条心跳均为 `success=false`
- **THEN** 产生独立的做市连续失败告警并显示三轮

### Requirement: 识别做市库存逼近上限
系统 MUST 在最新有效心跳的 `abs(inventory_usd)` 严格超过 `max_inv × 90%` 时
告警，并显示当前库存与上限。`inventory_usd=null` 时 MUST 跳过库存检查，既不
报错也不当作零值。

#### Scenario: 库存超过九成上限
- **WHEN** 最新库存名义额绝对值超过库存上限的 90%
- **THEN** 产生做市库存逼近上限告警并显示两个金额

#### Scenario: 库存估值不可用
- **WHEN** 最新心跳的 `inventory_usd` 为 `null`
- **THEN** 不产生库存告警且其他判据继续运行
