## ADDED Requirements

### Requirement: 提供安全默认的做市 CLI
系统 MUST 提供 Lighter 做市 CLI；未传 `--live` 时 MUST 使用 dry-run。标的、
每格名义、每边档位、库存上限、格距、快轮询秒数、慢路径秒数和 K 线账户前缀
MUST 分别默认为 `BTC`、`50`、`4`、`500`、`0.000986`、`2.5`、`30` 和
`X10_HEDGE`。Lighter 地址 MUST 支持通过 `--lighter-address` 指定，并默认读取
`LIGHTER_RH_L1_ADDRESS`。状态路径 MUST 支持通过 `--state-path` 指定并默认使用
`data/lighter_mm/state.json`。启动时 MUST 创建状态文件的父目录（包括缺失的多级
父目录）。趋势感知策略 MUST 由 `--trend-aware` 显式启用，
默认 MUST 保持关闭。

#### Scenario: 使用默认参数
- **WHEN** 用户未传参数
- **THEN** CLI 使用上述默认值并保持 `live=False`

#### Scenario: 显式启用趋势感知
- **WHEN** 用户传入 `--trend-aware`
- **THEN** 网格配置收到 `trend_aware=True`

#### Scenario: 首次启动时状态目录不存在
- **WHEN** 状态文件的父目录尚未创建且其他启动参数合法
- **THEN** CLI 递归创建该父目录后再装配客户端与网格引擎

### Requirement: 启动前执行资金安全自检
系统 MUST 拒绝非正档位、超过 500 USD 硬顶的库存、低于 15 USD 的每格名义，
以及每格名义乘档位超过库存上限的参数。乘积刚好等于库存上限时 MUST 放行。
实盘模式下缺少 `LIGHTER_API_PRIVATE_KEY` 时 MUST 拒绝启动。所有拒绝原因 MUST
使用中文并通过 `sys.exit` 结束。Lighter 地址为空时 MUST 在构造客户端前拒绝。
状态路径的父目录经真实路径解析后若与 `data/grid_state.json` 的父目录相同，也
MUST 在构造客户端前拒绝，并说明 `equity_peak.json`、`fills.jsonl` 和
`grid_live.json` 会与 Extended 网格撞车。

#### Scenario: 参数越过安全边界
- **WHEN** 任一安全条件不满足
- **THEN** CLI 在构造客户端前以中文原因退出

#### Scenario: 单边库存等于上限
- **WHEN** `unit * levels == max_inv` 且其他条件合法
- **THEN** 自检正常返回

#### Scenario: 同目录的不同主状态文件
- **WHEN** 状态路径为 `data/lighter_mm_state.json` 或 `data/grid_state.json`
- **THEN** 自检拒绝启动并说明三个派生文件会与 Extended 网格撞车

#### Scenario: 绝对路径绕过状态目录隔离
- **WHEN** 状态路径使用绝对路径指向 `data/` 下任意文件
- **THEN** 自检仍拒绝启动并说明三个派生文件会与 Extended 网格撞车

### Requirement: 以明确交易开关装配网格引擎
系统 MUST 使用命令行或环境地址、私钥环境变量和默认值为 255 的 API key index
构造 `LighterClient`。`trading_enabled` MUST 等于 `args.live`，使 dry-run 时交易
能力在适配器边界关闭。系统 MUST 按 CLI 参数构造 `GridConfig`，并使用
`ExtendedClient.from_env(args.candle_account)` 与固定 `market_override="BTC-USD"`
构造 `ExtendedCandleSource` 后注入 `GridEngine`。

#### Scenario: 装配默认 dry-run 引擎
- **WHEN** 自检通过且未传 `--live`
- **THEN** Lighter 客户端收到 `trading_enabled=False`，网格配置收到
  `dry_run=True`，行情源固定读取 Extended 的 `BTC-USD`

### Requirement: 每轮追加做市心跳
系统 MUST 在每轮结束后向 `data/lighter_mm.jsonl` 追加一行 JSON。快照 MUST
至少包含时间戳、标的、dry-run 状态、每边档位、每格名义、库存上限、当前持仓
数量、当前挂单数和本轮成功状态。失败轮次也 MUST 留下 `success=False` 的心跳，
且心跳写入失败 MUST NOT 覆盖原始交易轮次异常。

#### Scenario: 无网络验证完整心跳
- **WHEN** 使用本地测试替身执行一个成功轮次
- **THEN** 临时心跳文件新增一行且包含全部必需字段

### Requirement: 启动信息可人工核对
系统 MUST 使用名称为 `lighter_mm` 的 logger，并在启动时用一行中文日志汇总
标的、档位、每格金额、库存上限、格距、轮询间隔和 dry-run 状态。

#### Scenario: 启动摘要
- **WHEN** 引擎开始运行前
- **THEN** 日志中出现全部必需参数且只占一行

### Requirement: 不引入对冲互锁
系统 MUST NOT 导入不存在的 `grid.interlock`，MUST NOT 提供
`--require-hedge` 参数，也 MUST NOT 实现任何对冲逻辑。

#### Scenario: 查看入口能力
- **WHEN** 用户查看 CLI 帮助或入口依赖
- **THEN** 不存在对冲互锁参数或模块依赖
