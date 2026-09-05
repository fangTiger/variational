## ADDED Requirements

### Requirement: Swap 工具标识构造
系统 SHALL 支持构造 `instrument_type = "swap"` 的工具标识，且 SHALL 保证
`funding_interval_s = 0` 并强制携带 `kind`（取值 = 元数据中的 `asset_class`）。
`_instrument` 的 `funding_interval_s` SHALL NOT 使用非法的非零默认值。

#### Scenario: 构造黄金 swap 标识
- **WHEN** 调用方请求 `XAUS` 的工具标识
- **THEN** 返回 `{underlying:"XAUS", instrument_type:"swap", settlement_asset:"USDC", kind:"commodity", funding_interval_s:0}`

#### Scenario: 自动从元数据补全 kind
- **WHEN** 调用方只传 `underlying="XAUS"`，未指定 `instrument_type` 与 `kind`
- **THEN** 系统解析出 `("swap", "commodity")` 并补全两者
- **AND** 不再出现 `HTTP 400 missing field kind`

#### Scenario: 拒绝非法结算周期
- **WHEN** 对 swap 工具传入非 0 的 `funding_interval_s`
- **THEN** 系统在发请求前抛出明确错误，而非让后端返回 `unsupported instrument`

#### Scenario: 默认值不得为非法值
- **WHEN** 调用方未显式传 `funding_interval_s`
- **THEN** 系统按元数据解析该标的的正确取值
- **AND** 不得沿用固定的 3600 默认值

### Requirement: Swap 资金费查询
系统 SHALL 通过 `GET /funding/swap?underlying=<X>` 查询 swap 资金费。
`get_funding_rate()` SHALL 只接受永续工具；对 swap 工具调用 SHALL 抛出错误，
SHALL NOT 静默路由。

#### Scenario: 查询 XAUS 资金费
- **WHEN** 调用方查询 `XAUS` 的 swap 资金费
- **THEN** 返回 upcoming 与 latest_applied 两组费率
- **AND** `long_rate` 为负表示多头付款，`short_rate` 为正表示空头收款

#### Scenario: 对 swap 误用永续接口
- **WHEN** 调用方对 swap 工具调用 `get_funding_rate()`
- **THEN** 抛出指明应使用 `get_swap_funding()` 的错误

#### Scenario: 按日历天数判定覆盖天数
- **WHEN** 某次 `apply_time` 与上一次 `apply_time` 相隔 3 个日历天
- **THEN** 系统判定 `coverage_days = 3`
- **AND** SHALL NOT 依据「费率幅度约为近期值的 3 倍」来判定覆盖天数

#### Scenario: 费率突变必须告警而非被吞掉
- **WHEN** 某次费率幅度约为近期基准的 3 倍，但 `apply_time` 间隔仅 1 个日历天
- **THEN** 系统判定为费率突变并告警
- **AND** SHALL NOT 将其解释为多日计提

#### Scenario: 预测与实际扣款对账
- **WHEN** 一次资金费结算完成
- **THEN** 系统用 `/transfers` 的实际扣款与预测值对账
- **AND** 偏差超过阈值时告警

### Requirement: 资金费单位口径
系统 SHALL 将 `predicted_funding_rate` 与 swap 的 `long_rate`/`short_rate`
解释为**年化小数**；单期费率 SHALL 为 `年化值 ÷ 每年周期数`。
系统 SHALL 分别持久化 `raw_rate`、`coverage_days`、`day_count_basis`、
`normalized_annual_rate`、`observed_at`，SHALL NOT 只保存归一化后的值。

#### Scenario: 永续年化费率换算
- **WHEN** `/funding/v2` 对 BTC 返回 `predicted_funding_rate = 0.066721`、`funding_interval_s = 28800`
- **THEN** 系统报告年化 6.6721%
- **AND** 单期费率为 `0.066721 / 1095`，而非 `0.066721%`

#### Scenario: 与真实结算记录一致
- **WHEN** 用 `funding_rate = 0.0000685680365296804`、仓位 `+0.047801`、
  扣款 `-0.264342 USDC` 的真实记录校验
- **THEN** `扣款 = 费率 × 名义` 成立
- **AND** `费率 × 1095` 还原为年化 0.075082

#### Scenario: 拒绝旧口径
- **WHEN** 任何调用点仍按「百分比 / 结算周期」解释该字段
- **THEN** 对应测试失败

#### Scenario: 跨口径变更点的历史数据
- **WHEN** 回算 2026-01-01 之前的历史资金费
- **THEN** 系统拒绝使用当前公式或显式标注该段数据不可比
- **AND** 理由是平台在 2025-12-20/21 前后变更过口径

#### Scenario: Extended 侧单位同步校准
- **WHEN** 修正 Variational 侧口径后重算现有 BTC 结构的推荐方向
- **THEN** 系统同时按校准后的 Extended 口径计算
- **AND** 若推荐方向发生翻转，SHALL 显式告警而非静默改变建议

### Requirement: Swap 交易时段感知
系统 SHALL 从 `supported_assets` 读取 swap 的 `trading_sessions`、
`trading_schedule` 与 `market_status`，并对外提供「当前是否可交易」、
「下次休市时间」、「下次开市时间」、「距下次休市剩余时长」。

#### Scenario: 交易时段内
- **WHEN** 当前时间落在某 `trading_sessions` 区间内且 `market_status = "open"`
- **THEN** 报告可交易，并给出下次休市时间

#### Scenario: 周末长休市
- **WHEN** 当前时间为周五 21:00Z 之后、周日 22:00Z 之前
- **THEN** 报告不可交易，并报告本次休市时长约 49 小时

#### Scenario: 元数据缺失或陈旧时保守处理
- **WHEN** `trading_sessions` 为空、无法解析，或最新会话结束时间早于当前时间
- **THEN** 报告不可交易，而不是默认可交易

#### Scenario: 节假日导致休市长于预期
- **WHEN** `trading_schedule.next_open_at` 距当前超过 49 小时
- **THEN** 系统按实际值判定休市窗口，SHALL NOT 硬编码 49 小时

### Requirement: 保证金与强平价读取
系统 SHALL 直接读取 API 返回的 `estimated_liquidation_price`（`/positions`）
与 `estimated_liquidation_price_bid/ask`（`/quotes/indicative`）。
系统 SHALL NOT 用写死的维持保证金率与账户权益自行推算 isolated 腿的强平价。

#### Scenario: 读取 isolated 腿强平价
- **WHEN** 查询 XAUS 持仓的强平距离
- **THEN** 返回 API 提供的强平价
- **AND** 不得把账户总权益计入该 isolated 桶

#### Scenario: 区分两腿保证金模式
- **WHEN** 同时查询 XAUS 与 XAU 的保证金要求
- **THEN** 系统识别出 XAUS 为 `isolated`、XAU 无 `margin_mode` 字段即全仓
- **AND** 风控按各自模式分别计算缓冲

### Requirement: 仓位查询精确匹配
本能力涉及的所有仓位查询 SHALL 按 `instrument.underlying` 精确匹配，
SHALL NOT 使用子串匹配。

#### Scenario: XAU 不得命中 XAUS
- **WHEN** 账户同时持有 XAU 永续与 XAUS swap，查询 `underlying = "XAU"`
- **THEN** 只返回 XAU 永续持仓
- **AND** 不返回也不合并 XAUS 持仓
