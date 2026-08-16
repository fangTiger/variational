## Why

Robinhood Chain 上的 Lighter 仓位由人工维护，现有引擎缺少安全的公开只读适配器，也没有限制机器人自动跟随的 primary 名义金额。若方向解析错误、读取失败被误判为空仓，或人工误开超额仓位，自动对冲都可能扩大为裸仓或保证金风险。

## What Changes

- 新增 Lighter 公开 API 只读适配器，明确持仓方向、空仓、接口失败和未开户语义。
- 明确禁止机器人通过该适配器下单或平仓，人工腿始终只读。
- 为对冲引擎新增可选的 primary 名义金额上限，超限时保持两腿仓位不动。
- 用配置的异常类型解除引擎对 `VariationalAuthError` 类名的硬编码，同时保留旧 Variational 会话自愈能力。
- 所有新增行为通过无真实网络的失败路径优先测试验证。

## Capabilities

### New Capabilities

- `lighter-readonly-adapter`: 从 Robinhood Chain Lighter 公开接口安全读取账户、行情与持仓，并拒绝任何自动交易。
- `hedge-engine-safety`: 为通用对冲引擎增加可选名义上限和可配置的自愈异常类型。

### Modified Capabilities

无。

## Impact

- 新增 `adapters/lighter_client.py`、`tests/test_lighter_client.py` 和 `tests/test_hedge_notional_cap.py`。
- 修改 `adapters/base.py` 的交易能力标识、`engine/hedge_engine.py` 的配置与单轮执行流程，以及旧 Variational 入口的异常类型配置。
- 依赖现有 `httpx`，不新增第三方依赖，不修改公共交易入口、`.env`、`tools/run_grid.py` 或 `grid/`。
- 本变更仅覆盖既有实施计划的 Task 1 和 Task 2；账户凭据、CLI、告警和上线均不在范围内。
