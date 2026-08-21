# hedge-shared-account-guard

## ADDED Requirements

### Requirement: 共用网格账户需显式声明

对冲入口 SHALL 仅在显式声明后才允许使用与网格相同的账户或 vault。

#### Scenario: 未声明时维持拒绝
- **WHEN** 未传共用声明且账户与网格账户或 vault 相同
- **THEN** 拒绝启动，行为与本变更前一致

#### Scenario: 声明后允许进入校验
- **WHEN** 传入共用声明
- **THEN** 不因账户前缀或 vault 相同而直接拒绝，转入运行时状态校验

#### Scenario: 默认关闭
- **WHEN** 未传任何参数
- **THEN** 视为未声明，保持隔离

### Requirement: 共用账户须通过运行时状态校验

对冲入口 SHALL 在共用模式下、对冲引擎构造前，使用默认不启用交易能力的
Lighter 客户端读取 primary 持仓，并通过 Extended 适配器读取账户全部持仓。
选定对冲标的的净敞口 SHALL 定义为 `primary_size + hedge_size`，单腿基数 SHALL
定义为 `max(abs(primary_size), abs(hedge_size))`；当且仅当
`abs(net_delta) <= rebalance_threshold * leg_base` 时，该持仓 SHALL 被视为
近似互为反向的合法对冲。容差比例 SHALL 直接取既有 `--rebalance-threshold`，
默认值为 `0.02`，不得另行硬编码启动专用魔数。

Extended 其他市场达到可交易最小量的持仓因没有对应 Lighter primary 可解释，
SHALL 被拒绝。Extended 绝对值低于对应市场最小下单量且没有非零 primary 的
不可交易微仓 SHALL 继续被忽略。任一侧持仓读取失败 SHALL 保守拒绝。目标账户
存在普通网格挂单时 SHALL 始终拒绝，无论两腿持仓是否满足对冲判据。

#### Scenario: 账户干净时放行
- **WHEN** 共用声明已给出、两腿持仓均为零且无普通网格挂单
- **THEN** 允许启动

#### Scenario: 事故仓位构成合法对冲时放行
- **WHEN** Lighter BTC 持仓为 `-0.01972`，Extended BTC-USD 持仓为 `+0.01972`，`rebalance_threshold=0.02`，且无普通网格挂单
- **THEN** 净敞口为 `0`，在容差内，允许启动

#### Scenario: 对冲误差在既有再平衡容差内时放行
- **WHEN** 两腿反向，且净敞口绝对值不超过 `0.02 * max(abs(primary_size), abs(hedge_size))`
- **THEN** 允许启动

#### Scenario: 不可交易微仓时放行
- **WHEN** Lighter 持仓为零，Extended 仅有绝对值低于对应市场最小下单量的持仓，且无普通网格挂单
- **THEN** 忽略该微小持仓并允许启动

#### Scenario: Extended 仓位没有 primary 解释时拒绝
- **WHEN** Extended 有达到最小下单量的持仓而对应 Lighter primary 为零
- **THEN** 拒绝启动，错误信息包含 Lighter 数量、Extended 数量、净敞口与容差

#### Scenario: 两腿持仓同向时拒绝
- **WHEN** Lighter 与 Extended 持仓方向相同，导致净敞口超过容差
- **THEN** 拒绝启动，错误信息包含两侧数量与净敞口

#### Scenario: 非目标市场存在可交易仓位时拒绝
- **WHEN** Extended 其他市场存在达到最小下单量、无法映射到当前 Lighter primary 的持仓
- **THEN** 将对应 primary 数量按零处理并拒绝启动，错误信息包含两侧数量与净敞口

#### Scenario: 存在网格挂单时拒绝
- **WHEN** 共用声明已给出且存在普通网格挂单，无论持仓是否构成合法对冲
- **THEN** 拒绝启动并说明该账户仍有网格活动

#### Scenario: 任一侧持仓读取失败时保守拒绝
- **WHEN** Lighter 或 Extended 任一持仓查询失败或返回不可用结果
- **THEN** 拒绝启动并指出无法确认哪一侧的持仓状态

#### Scenario: 最小下单量查询失败时保守回退
- **WHEN** 共用声明已给出但 Extended 适配器无法返回某持仓市场的有效最小下单量
- **THEN** 使用保守的极小阈值继续校验，并在日志中说明市场、回退阈值与查询错误

### Requirement: 共用模式在启动摘要中可见

对冲入口 SHALL 在启动摘要中标注账户处于共用模式。

#### Scenario: 摘要标注共用
- **WHEN** 以共用模式启动
- **THEN** 启动摘要显示该账户与网格共用，并提示网格进程不得同时运行
