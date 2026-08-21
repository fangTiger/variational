# timed-hedged-volume

## ADDED Requirements

### Requirement: 按固定周期开平仓

策略 SHALL 按可配置的固定周期开仓，并在持仓满该周期后平仓。

#### Scenario: 到期开仓
- **WHEN** 当前无持仓且已到开仓时刻
- **THEN** 在 Lighter 开出配置的名义额仓位

#### Scenario: 持仓未满周期不平仓
- **WHEN** 持仓时长未达到配置周期
- **THEN** 不执行平仓

#### Scenario: 持仓满周期后平仓
- **WHEN** 持仓时长达到配置周期
- **THEN** 平掉该仓位，并同步平掉对冲腿

#### Scenario: 周期内不追加交易
- **WHEN** 已有持仓且未到期
- **THEN** 不产生任何新的开仓委托

### Requirement: 开仓方向逐轮交替

策略 SHALL 使每一轮的开仓方向与上一轮相反。

#### Scenario: 首轮方向可配
- **WHEN** 没有历史轮次
- **THEN** 采用配置的初始方向

#### Scenario: 后续轮次交替
- **WHEN** 上一轮为做多
- **THEN** 本轮为做空，反之亦然

#### Scenario: 重启后保持交替序列
- **WHEN** 进程重启且存在历史轮次记录
- **THEN** 依据记录延续交替，不重复上一轮方向

### Requirement: 两侧同步建立与解除对冲

策略 SHALL 在开仓时于 Extended 建立反向仓、平仓时同步解除，使净敞口归零。

#### Scenario: 开仓后净敞口为零
- **WHEN** 一轮开仓完成
- **THEN** 两侧持仓互为反向且净敞口在容差内

#### Scenario: 平仓后两侧均归零
- **WHEN** 一轮平仓完成
- **THEN** 两侧持仓均为零

#### Scenario: 下单采用 maker 优先
- **WHEN** 建立或解除任一侧仓位
- **THEN** 先以限价单尝试成交，超时后才使用市价单

### Requirement: 单边成交必须收敛

策略 SHALL 在仅一侧成交时收敛敞口，不得保留单边裸仓。

#### Scenario: 对冲侧未成交则回滚开仓侧
- **WHEN** Lighter 已开仓但 Extended 对冲在超时后仍未建立
- **THEN** 平掉 Lighter 已开仓位，使净敞口归零，并记录中文告警

#### Scenario: 开仓侧未成交则撤销对冲侧
- **WHEN** Extended 已建立对冲但 Lighter 开仓未成交
- **THEN** 平掉 Extended 仓位，使净敞口归零

#### Scenario: 部分成交按实际仓位补齐
- **WHEN** 任一侧仅部分成交
- **THEN** 依据两侧实际持仓差补齐或回滚，不得按委托量推断

### Requirement: 轮次状态持久化并可恢复

策略 SHALL 持久化轮次状态，使进程重启后能延续当前轮次。

#### Scenario: 记录当前轮次
- **WHEN** 一轮开仓完成
- **THEN** 持久化该轮方向与到期时刻

#### Scenario: 重启后恢复未到期轮次
- **WHEN** 进程重启且记录显示存在未到期持仓
- **THEN** 不重复开仓，等待原到期时刻平仓

#### Scenario: 重启后发现已过期则立即平仓
- **WHEN** 进程重启且记录显示持仓已过期
- **THEN** 立即执行平仓流程

#### Scenario: 状态与实际持仓不一致时以实际为准
- **WHEN** 持久化状态与两侧实际持仓不符
- **THEN** 以实际持仓为准收敛敞口，并记录中文告警

### Requirement: 对冲不可用时不得开新仓

策略 SHALL 在对冲不可用时停止开仓，但仍允许平仓与撤单。

#### Scenario: 对冲失效时跳过开仓
- **WHEN** 到达开仓时刻但对冲存活判定为失效
- **THEN** 不开仓，记录中文告警并等待下一次判定

#### Scenario: 对冲失效仍允许平仓
- **WHEN** 已有持仓到期且对冲判定为失效
- **THEN** 仍执行平仓，避免敞口滞留
