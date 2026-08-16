## Why

Lighter RH 的人工 primary 仓位已经具备只读适配器和通用对冲引擎，但缺少一个默认安全、账户隔离且可直接运行的 CLI。若错误复用正在实盘运行的网格账户，两个策略会互相误读持仓并破坏风控，因此入口必须在连接客户端前拒绝危险配置。

## What Changes

- 新增 Lighter RH 积分对冲 CLI，默认 dry-run，并开放市场、轮询、再平衡阈值和 primary 名义上限参数。
- 从命令行或 `LIGHTER_RH_L1_ADDRESS` 读取 Robinhood Wallet 地址。
- 在客户端构造前拒绝 `X10_GRID` 前缀和与网格相同的 vault ID。
- 使用只读 `LighterClient` 与独立前缀的 `ExtendedClient` 装配 `HedgeEngine`，关闭认证自愈。
- 启动时输出两腿账户标识、Lighter account index、标的、名义上限和 dry-run 状态，并写入专用日期日志。

## Capabilities

### New Capabilities

- `lighter-hedge-cli`: 定义 Lighter RH 对冲 CLI 的参数、安全自检、引擎装配、启动可观测性和资源回收。

### Modified Capabilities

无。

## Impact

- 新增 `tools/run_lighter_hedge.py` 与对应无网络测试。
- 更新 README 的系统入口与账户隔离说明；复用已有 `.env.example` 中的 `X10_HEDGE_*` 和 `LIGHTER_RH_L1_ADDRESS` 配置。
- 不修改网格实现、运行时 `.env`、告警或上线配置，不新增第三方依赖。
