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

对冲入口 SHALL 在共用模式下校验目标账户无持仓且无网格挂单。

#### Scenario: 账户干净时放行
- **WHEN** 共用声明已给出且目标账户持仓为零、无网格挂单
- **THEN** 允许启动

#### Scenario: 存在持仓时拒绝
- **WHEN** 共用声明已给出但目标账户存在非零持仓
- **THEN** 拒绝启动并说明该账户仍有仓位

#### Scenario: 存在网格挂单时拒绝
- **WHEN** 共用声明已给出但目标账户存在网格挂单
- **THEN** 拒绝启动并说明该账户仍有网格活动

### Requirement: 共用模式在启动摘要中可见

对冲入口 SHALL 在启动摘要中标注账户处于共用模式。

#### Scenario: 摘要标注共用
- **WHEN** 以共用模式启动
- **THEN** 启动摘要显示该账户与网格共用，并提示网格进程不得同时运行
