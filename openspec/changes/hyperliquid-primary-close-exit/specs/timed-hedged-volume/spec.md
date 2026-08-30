## ADDED Requirements

### Requirement: Hyperliquid 可作为主腿
系统 SHALL 允许主腿和对冲腿分别使用同一 Hyperliquid 账户的不同 builder dex 市场。

#### Scenario: 按独立前缀装配主腿
- **WHEN** 操作员指定 `--primary-venue hyperliquid --primary-env-prefix PREFIX`
- **THEN** 系统 SHALL 使用 `PREFIX` 构造启用交易的 Hyperliquid 客户端
- **AND** SHALL 从 `PREFIX_ACCOUNT_ADDRESS` 读取主腿账户标识
- **AND** 启动摘要 SHALL 显示主腿场馆与该环境变量前缀

#### Scenario: 同账户不同市场不冲突
- **WHEN** 主腿和对冲腿使用同一 Hyperliquid 账户，市场分别为 `xyz:SNDK` 与 `io:SNDK`
- **THEN** 系统 SHALL 允许两腿并存
- **AND** 市场占用检查 SHALL NOT 将其判为同一占用

### Requirement: 只平仓并退出
系统 SHALL 提供只结束当前轮次且不打开新轮次的退出模式。

#### Scenario: 初始没有进行中轮次
- **WHEN** `--close-and-exit` 开启且持久化状态没有进行中轮次
- **THEN** 系统 SHALL 记录已平仓日志并退出
- **AND** SHALL NOT 调用一次 `run_once`

#### Scenario: 当前轮次立即平仓
- **WHEN** `--close-and-exit` 开启且持久化状态有进行中轮次
- **THEN** 系统 SHALL 把 `due_at` 更新为当前时刻并立即持久化
- **AND** SHALL 推进状态机直至当前轮次平仓成功

#### Scenario: 平仓成功后不再推进
- **WHEN** 平仓节拍返回 `closed` 或持久化状态已没有进行中轮次
- **THEN** 系统 SHALL 退出循环
- **AND** SHALL NOT 再次调用 `run_once`
- **AND** 退出前 SHALL 记录最终双腿持仓与净敞口

#### Scenario: 平仓失败继续安全重试
- **WHEN** 平仓节拍返回 `close_failed_neutral` 或 `close_halted`
- **THEN** 系统 SHALL 按既有紧急重试与退避机制继续尝试
- **AND** SHALL NOT 打开新轮次
