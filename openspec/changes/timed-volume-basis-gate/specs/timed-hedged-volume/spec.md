## ADDED Requirements

### Requirement: 新开仓前按相对基差门控

系统 SHALL 仅在空仓准备开新轮次时，以两腿当前中价计算基差，并以单实例最近
20 轮开仓基差的中位数为中心判断相对偏离。门控 SHALL NOT 影响持仓轮次的到期平仓。

#### Scenario: 相对偏离超过阈值
- **WHEN** 历史样本不少于五轮、标准差不低于 0.02%，且当前基差相对历史中位数的绝对偏离超过配置的标准差倍数
- **THEN** 系统 SHALL 不调用交易执行器
- **AND** 门控状态 SHALL 为 `waiting`

#### Scenario: 偏离恢复
- **WHEN** 等待中的基差回到阈值内
- **THEN** 系统 SHALL 正常开仓
- **AND** 门控状态 SHALL 为 `open`

#### Scenario: 等待达到上限
- **WHEN** 累计等待达到 `basis_gate_max_wait_s`
- **THEN** 系统 SHALL 无条件尝试开仓
- **AND** 门控状态 SHALL 为 `forced`

#### Scenario: 历史无法可靠估计
- **WHEN** 历史少于五轮或历史标准差低于 0.02%
- **THEN** 系统 SHALL 直接开仓

#### Scenario: 门控关闭
- **WHEN** `basis_gate_sigma` 为零
- **THEN** 系统 SHALL 保持既有开仓行为，且不增加门控行情或历史读取

#### Scenario: 持仓到期
- **WHEN** 持仓轮次已经到期
- **THEN** 系统 SHALL 按既有语义平仓，不评估开仓基差门控

### Requirement: 基差门控可观测

系统 SHALL 在每条心跳中输出当前偏离、当前轮累计等待秒数和门控状态。

#### Scenario: 强制开仓可统计
- **WHEN** 因等待超时强制开仓
- **THEN** 心跳的 `basis_gate_state` SHALL 为 `forced`
- **AND** `basis_gate_waited_seconds` SHALL 表示本轮累计等待秒数

