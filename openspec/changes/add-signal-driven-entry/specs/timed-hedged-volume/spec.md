## ADDED Requirements

### Requirement: 入场模式向后兼容

系统 SHALL 提供 `timer` 与 `signal` 两种入场模式，并默认使用 `timer`。

#### Scenario: 默认定时模式
- **WHEN** 用户未传入 `--entry-mode`
- **THEN** 系统 SHALL 保持既有到期、交替方向和旧基差门控行为
- **AND** SHALL NOT 拉取信号历史 K 线

### Requirement: 基差信号统计来自 Hyperliquid 历史 K 线

系统 SHALL 按配置窗口分别通过 Hyperliquid `candleSnapshot` 读取两腿 5 分钟 K 线，
按 `t` 对齐收盘价后计算百分比基差的中位数与总体标准差，并按配置间隔刷新。

#### Scenario: 样本不足
- **WHEN** 对齐后的有效样本少于 `signal_min_samples`
- **THEN** 系统 SHALL 拒绝新开仓
- **AND** 心跳 SHALL 标明样本数和拒绝原因
- **AND** 定时兜底 SHALL NOT 绕过该拒绝

### Requirement: 信号方向由偏离方向决定

系统 SHALL 在当前基差偏离大于正阈值时让主腿做空、对冲腿做多，在偏离小于负阈值时
让主腿做多、对冲腿做空，阈值内不交易。

#### Scenario: 正偏离
- **WHEN** `deviation > signal_sigma * sigma`
- **THEN** 系统 SHALL 开空基差

#### Scenario: 负偏离
- **WHEN** `deviation < -signal_sigma * sigma`
- **THEN** 系统 SHALL 开多基差

#### Scenario: 阈值内
- **WHEN** 偏离未严格越过任一阈值
- **THEN** 系统 SHALL 不开仓

### Requirement: 信号仓按回归或超时平仓

系统 SHALL 在信号入场偏离越过当前中位线，或持仓达到 `max_hold_hours` 时平仓。

#### Scenario: 正偏离回归
- **WHEN** 空基差仓的当前偏离小于等于零
- **THEN** 系统 SHALL 平仓并记录 `reverted`

#### Scenario: 负偏离回归
- **WHEN** 多基差仓的当前偏离大于等于零
- **THEN** 系统 SHALL 平仓并记录 `reverted`

#### Scenario: 最长持仓
- **WHEN** 非兜底信号仓达到最长持仓时间
- **THEN** 系统 SHALL 强制平仓并记录 `timeout`

### Requirement: 无信号定时兜底

系统 SHALL 在已有可靠统计但持续无入场信号达到配置时长后，按既有定时方向开一轮；
配置为零时 SHALL 禁用兜底。

#### Scenario: 达到兜底时长
- **WHEN** 空仓无信号持续达到 `signal_fallback_hours`
- **THEN** 系统 SHALL 按既有交替方向开仓并标记 `fallback`

#### Scenario: 兜底轮结束
- **WHEN** 兜底轮达到信号模式最长持仓时间
- **THEN** 系统 SHALL 强制平仓并记录 `fallback`

#### Scenario: 兜底关闭
- **WHEN** `signal_fallback_hours` 为零
- **THEN** 系统 SHALL 不因等待时长开仓

### Requirement: 信号模式不削弱既有保护

系统 SHALL 在信号模式继续执行平仓退出、撤销两腿挂单、连续平仓失败退避和认证互锁自愈。

#### Scenario: 平仓退出
- **WHEN** 信号模式带 `--close-and-exit` 启动
- **THEN** 系统 SHALL 平掉当前轮、撤销两腿挂单并退出
- **AND** SHALL NOT 开新仓

### Requirement: 信号决策可审计

系统 SHALL 在启动摘要打印全部信号参数，并在轮次台账记录开仓时的偏离、中位线、
标准差与平仓原因。

#### Scenario: 信号轮完成
- **WHEN** 信号模式轮次成功平仓
- **THEN** 台账 SHALL 包含入场 `deviation`、`midline`、`sigma`
- **AND** SHALL 包含 `reverted`、`timeout` 或 `fallback` 平仓原因
