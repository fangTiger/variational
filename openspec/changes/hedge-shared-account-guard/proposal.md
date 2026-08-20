# Change: 对冲腿账户隔离改为显式知情 + 运行时校验

## Why

`run_lighter_hedge.py:274` 硬拒绝 `X10_GRID` 前缀，且拒绝任何与
`X10_GRID_VAULT_ID` 相同 vault 的账户。实测 `.env` 中
`X10_VAULT_ID = X10_GRID_VAULT_ID = 396762`，因此三个 Extended 账户里只有
`X10_HEDGE`（vault 398961）合法——而它余额为 **$0**，`X10_GRID` 有 **$840.10**。

该保护当初防的是「Extended 同时跑网格与对冲，两个策略仓位搅在一起」。但在
当前架构下 **Extended 不再跑网格、只做纯对冲腿**，前提已不成立：账户上不会有
网格持仓，对冲机器人镜像的就是它自己的仓位。

风险从「账户共用」转移到了「误启动网格进程」。因此应把静态黑名单换成
**显式知情声明 + 启动时实际状态校验**，与既有 `--waive-risk` 模式一致。

## What Changes

- 新增显式开关，允许对冲腿使用与网格相同的账户；**默认关闭**，不声明时
  维持现有拒绝行为。
- 声明后仍须通过运行时校验：目标账户**无持仓、无网格挂单**，否则拒绝启动。
- 启动摘要标注该账户为共用模式，使其在日志中可见。

## Capabilities

### New Capabilities

- `hedge-shared-account-guard`: 定义共用账户的显式声明、启动时状态校验、
  拒绝条件与可观测性。

### Modified Capabilities

无。未声明开关时账户隔离行为完全不变。

## Impact

- 修改 `tools/run_lighter_hedge.py`：新增开关与运行时校验。
- 修改 `adapters/extended_client.py`：提供不按标的过滤的账户级持仓与挂单读取。
- 新增无网络测试，失败路径优先。
- **不修改** plist；账户切换由人工在验收后配置。
- 不改动任何风控阈值与对冲算法。
