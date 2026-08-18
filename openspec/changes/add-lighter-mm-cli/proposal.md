## Why

Lighter 交易适配器与通用网格引擎已经具备做市所需能力，但目前缺少一个默认
安全、参数受限且能借用 Extended 行情的独立入口。直接复用现有入口容易把
dry-run 与实盘开关接反，或因 Lighter/Extended 标的名不同导致 K 线查询失败；
过大的库存参数和过小的订单名义额也会造成真实资金风险或静默拒单。

## What Changes

- 新增 Lighter 无对冲做市 CLI，默认 dry-run，并开放标的、档位、每格名义、
  库存上限、格距和快慢轮询间隔参数。
- 启动前校验档位、硬编码库存顶、最小每格名义、单边挂满库存以及实盘私钥。
- 用交易开关明确的 `LighterClient` 装配 `GridEngine`，并借用 Extended 的
  `BTC-USD` K 线作为行情源。
- 启动时通过 `lighter_mm` 专用日志输出一行参数摘要。
- 使用独立状态路径，拒绝复用 Extended 网格状态，并显式声明是否启用趋势感知。
- 每轮向 `data/lighter_mm.jsonl` 追加持仓、挂单和成功状态心跳。
- 不增加任何对冲互锁或对冲参数。

## Capabilities

### New Capabilities

- `lighter-mm-cli`: 定义 Lighter 无对冲网格入口的参数、安全自检、引擎装配和
  启动可观测性。

### Modified Capabilities

无。

## Impact

- 新增 `tools/run_lighter_mm.py` 与对应无网络测试。
- 复用 `adapters/lighter_client.py`、`adapters/market_data.py` 和
  `grid/grid_engine.py`，不修改这些文件。
- 不修改部署配置，不连接交易所，不引入第三方依赖。
