## ADDED Requirements

### Requirement: 系统状态提供可选总收益
`SystemStatus` MUST 提供默认值为 `None` 的 `total_pnl` 字段。未传入该字段的既有调用 MUST 继续正常工作。

#### Scenario: 旧调用保持兼容
- **WHEN** 调用方只按原有参数构造 `SystemStatus`
- **THEN** 对象成功创建且 `total_pnl is None`

### Requirement: 网格收益使用明确基线
网格 provider MUST 把原“自起始 PnL”替换为“总收益”，并优先使用当前权益减去 `grid_baseline.json` 中的有效 `equity`。展示值 MUST 带由有效 `ts` 格式化的 `MM-DD` 基线日期。基线缺失、损坏或字段无效时，provider MUST 回退 `pnl_since_start` 并显示“基线未知”。provider MUST 从 `attribution.json` 读取 `unrealised_end` 作为“当前持仓浮盈”，文件或字段不可用时显示 `—`。

#### Scenario: 有效网格基线
- **WHEN** 当前权益为 998.25，基线权益为 948.65，基线日期为 08-06
- **THEN** “总收益”显示 `$+49.60（自 08-06）`，且 `SystemStatus.total_pnl` 为 49.60

#### Scenario: 网格基线不可用
- **WHEN** 基线文件缺失或损坏且心跳含 `pnl_since_start`
- **THEN** “总收益”回退该值并追加 `（基线未知）`

#### Scenario: 当前持仓浮盈为负
- **WHEN** `unrealised_end` 为负数
- **THEN** “当前持仓浮盈”显示带符号金额且 tone 为 `bad`

### Requirement: 对冲总收益必须包含两条腿
对冲 provider MUST 使用心跳中的 `primary_collateral + hedge_equity` 减去 `hedge_baseline.json` 的有效 `total` 计算“总收益”，并展示有效 `ts` 的 `MM-DD` 日期。基线不可用或任一当前权益字段不可用时，“总收益” MUST 显示 `—` 且 `SystemStatus.total_pnl` MUST 为 `None`。对冲卡片 MUST 仅在 `primary_unrealized` 与 `hedge_unrealized` 都有效时显示两者之和作为“当前持仓浮盈”；任一缺失或无效时 MUST 显示 `—`。浮盈绝对值小于 5 美元、20 美元的 tone MUST 分别为 `good`、`warn`，其余为 `bad`。

#### Scenario: 两条腿和基线完整
- **WHEN** 两条腿当前权益都有效且基线 total 和 ts 有效
- **THEN** “总收益”显示带符号金额和基线日期，并填充 `total_pnl`

#### Scenario: 只有一条腿权益
- **WHEN** `primary_collateral` 或 `hedge_equity` 任一缺失
- **THEN** “总收益”显示 `—`，不得用单腿计算

#### Scenario: 对冲总收益为负
- **WHEN** 两腿当前权益合计小于基线 total
- **THEN** “总收益”tone 为 `bad`

#### Scenario: 两腿浮盈完整且合计为负
- **WHEN** `primary_unrealized` 为 `-1.25` 且 `hedge_unrealized` 为 `0.30`
- **THEN** “当前持仓浮盈”显示 `$-0.95` 且 tone 为 `good`

#### Scenario: 只有一条腿浮盈
- **WHEN** `primary_unrealized` 或 `hedge_unrealized` 任一缺失
- **THEN** “当前持仓浮盈”显示 `—`，不得用单腿数值计算

#### Scenario: 对冲浮盈 tone 边界
- **WHEN** 两腿浮盈之和绝对值等于 5 美元或 20 美元
- **THEN** tone 分别为 `warn` 或 `bad`

### Requirement: 总览汇总可计算的总收益
注册表 MUST 只累加数值型 `total_pnl`。渲染器 MUST 在总权益旁显示格式为 `总收益 $+47.75` 的合计；正值使用 `good`，负值使用 `bad`，零使用普通色。若任一系统的 `total_pnl` 不可计算，数值后 MUST 加 `*`，且该元素的 `title` MUST 说明未计入的系统名称。

#### Scenario: 部分系统未计入
- **WHEN** 两个系统有数值总收益，一个系统的 `total_pnl` 为 `None`
- **THEN** 总览仅汇总两个数值，在结果后显示 `*`，title 指出第三个系统未计入
