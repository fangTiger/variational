## ADDED Requirements

### Requirement: 提供安全默认的 CLI 参数
系统 MUST 提供 Lighter RH 对冲 CLI；未传 `--live` 时 MUST 使用 dry-run。账户前缀、两侧标的、primary 名义上限、轮询秒数和再平衡比例 MUST 分别默认为 `X10_HEDGE`、`BTC`、`BTC-USD`、`2000`、`30` 和 `0.02`。Lighter 地址 MUST 支持通过 `--lighter-address` 指定，并默认读取 `LIGHTER_RH_L1_ADDRESS`。

#### Scenario: 使用默认参数
- **WHEN** 环境中存在 `LIGHTER_RH_L1_ADDRESS` 且用户未传其他参数
- **THEN** CLI 使用上述默认值并配置 `dry_run=True`

#### Scenario: 显式启用实盘
- **WHEN** 用户传入 `--live`
- **THEN** CLI 配置 `dry_run=False`

### Requirement: 启动前隔离网格账户
系统 MUST 在构造客户端前规范化账户前缀并拒绝 `X10_GRID`。当 `X10_HEDGE_VAULT_ID` 与 `X10_GRID_VAULT_ID` 均存在且相同时，系统 MUST 拒绝启动；所选账户的 vault 与网格 vault 相同时也 MUST 拒绝启动。

#### Scenario: 传入网格前缀
- **WHEN** 规范化后的 `--account` 等于 `X10_GRID`
- **THEN** CLI 在构造 Lighter 或 Extended 客户端前报错退出

#### Scenario: 对冲与网格 vault 相同
- **WHEN** `X10_HEDGE_VAULT_ID` 等于 `X10_GRID_VAULT_ID`
- **THEN** CLI 在构造客户端前报错退出

#### Scenario: Lighter 地址缺失
- **WHEN** 命令行和环境变量都没有提供非空 Lighter 地址
- **THEN** CLI 在构造客户端前给出包含 `LIGHTER_RH_L1_ADDRESS` 的错误

### Requirement: 按只读 primary 模式装配引擎
系统 MUST 使用 `LighterClient` 作为 primary、`ExtendedClient.from_env(prefix=args.account)` 作为 hedge，并把 CLI 风险参数传入 `HedgeConfig`。配置的认证异常类型 MUST 为空元组，`HedgeEngine` 的认证错误回调 MUST 为 `None`。

#### Scenario: 装配默认 dry-run 引擎
- **WHEN** 启动自检通过且未传 `--live`
- **THEN** 引擎收到正确市场、轮询、再平衡比例、名义上限、`dry_run=True`、`auth_error_types=()` 和 `on_auth_error=None`

### Requirement: 启动信息可人工核对
系统 MUST 在 Lighter 连接成功后打印两腿账户标识、Lighter account index、两侧标的、primary 名义上限和 dry-run 状态，并 MUST 把入口日志写到 `log/lighter_hedge_YYYYMMDD.log`。

#### Scenario: 启动摘要
- **WHEN** Lighter 成功解析 account index
- **THEN** 控制台输出包含钱包地址、account index、Extended 前缀、标的映射、名义上限和 dry-run 状态

#### Scenario: 专用日志
- **WHEN** CLI 模块初始化 logger
- **THEN** 文件 handler 的名称为当天的 `lighter_hedge_YYYYMMDD.log`

### Requirement: 关闭两腿客户端
系统 MUST 在守护循环结束或异常退出时尝试关闭所有已构造的客户端，一个客户端关闭失败 MUST NOT 阻止另一个客户端关闭。

#### Scenario: 引擎退出
- **WHEN** `run_forever()` 返回或抛出异常
- **THEN** CLI 尝试关闭 Lighter 与 Extended 客户端
