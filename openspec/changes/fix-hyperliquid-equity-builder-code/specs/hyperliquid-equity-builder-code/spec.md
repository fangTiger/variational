# hyperliquid-equity-builder-code

## ADDED Requirements

### Requirement: 风控权益表示全平后的 USDC 可用资金

适配器 SHALL 将 Spot USDC 总额与全部永续持仓的未实现盈亏合计相加作为
`equity`，不得再把 `marginSummary.accountValue` 加入权益。

#### Scenario: 空仓权益等于 Spot USDC
- **WHEN** Spot USDC 为 457.31 且账户无持仓
- **THEN** `equity` 为 457.31

#### Scenario: 持仓浮盈计入权益
- **WHEN** Spot USDC 为 498.20 且全部持仓未实现盈亏合计为 5.47
- **THEN** `equity` 为 503.67

#### Scenario: 持仓浮亏扣减权益
- **WHEN** Spot USDC 为 500 且全部持仓未实现盈亏合计为 -30
- **THEN** `equity` 为 470

#### Scenario: 多个持仓逐项合计
- **WHEN** `assetPositions` 含多个持仓
- **THEN** 对每个 `position.unrealizedPnl` 严格解析后求和

#### Scenario: 非 USDC Spot 资产不计入
- **WHEN** Spot 账户还持有 HYPE、MAX、USDT0 或其他代币
- **THEN** 这些代币不影响 `equity`

#### Scenario: 保留账户诊断值
- **WHEN** 成功查询余额
- **THEN** 返回 `perps_account_value`、`spot_usdc_total` 与
  `unrealized_pnl_total`
- **AND** `perps_account_value` 仅供诊断，不加入 `equity`

#### Scenario: 任一必需查询失败时保守关闭
- **WHEN** 永续账户状态或 Spot 账户状态任一查询失败或返回必需结构无效
- **THEN** 抛出中文上下文异常，不得用另一侧结果降级返回权益

#### Scenario: 合计权益非正时拒绝返回
- **WHEN** Spot USDC 与全部持仓未实现盈亏合计不大于零
- **THEN** 抛出异常，不得返回无效权益

### Requirement: 可选 Builder Code 归属所有下单

适配器 SHALL 支持可选的 Hyperliquid builder 地址和费率配置。启用后，所有限价
单与市价单 SHALL 通过 SDK `Exchange.order` 的 `builder` 参数传递
`{"b": 地址, "f": 费率}`。

#### Scenario: 默认不启用 builder
- **WHEN** 两个 builder 环境变量均未配置
- **THEN** 限价单与市价单调用中不提供 `builder` 参数
- **AND** 既有下单行为保持不变

#### Scenario: 环境变量启用 builder
- **WHEN** 配置 `HYPERLIQUID_BUILDER_ADDRESS` 与
  `HYPERLIQUID_BUILDER_FEE_TENTHS_BPS`
- **THEN** 限价单与市价单都透传相同的 `{"b", "f"}` 对象

#### Scenario: 零费率合法
- **WHEN** builder 费率配置为整数 0
- **THEN** 构造成功且下单时保留 `"f": 0`

#### Scenario: 非法地址在构造阶段失败
- **WHEN** builder 地址不是 `0x` 加 40 位十六进制字符
- **THEN** 构造立即失败并给出中文地址错误

#### Scenario: 非法费率在构造阶段失败
- **WHEN** builder 费率不是整数、为负数或超过永续上限 100（0.1bp 单位）
- **THEN** 构造立即失败并给出中文费率原因

#### Scenario: builder 配置必须完整
- **WHEN** 地址与费率只配置其中一项
- **THEN** 构造立即失败并给出中文配置不完整错误
