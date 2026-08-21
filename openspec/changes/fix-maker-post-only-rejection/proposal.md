## Why

Hyperliquid 会把下单瞬间可能立即成交的 post-only 委托作为业务错误返回，而 maker 优先对冲当前把该错误直接向上抛出，导致平仓重试并阻断后续反向开仓。该问题会在每次实盘轮转中重复出现，必须把这种可预期竞态降级为安全的市价补差。

## What Changes

- maker 优先挂单遇到 post-only 立即成交类拒绝时，不再让整轮失败，改为进入市价补差路径。
- 降级前重读真实持仓，只按目标变化量与实际持仓变化量之差补单，不按原委托量盲目吃单。
- 使用大小写不敏感的多关键片段识别不同交易所措辞；无关下单错误继续按原路径向上抛出。
- 保持 maker 正常成交、超时撤单、订单查询降级、再平衡门禁和风控行为不变。

## Capabilities

### New Capabilities

- `post-only-rejection-fallback`: 规定 maker 优先执行遇到 post-only 立即成交类拒绝时的识别、安全补差和错误边界。

### Modified Capabilities

无。当前仓库没有已发布到 `openspec/specs/` 的主规范。

## Impact

- 修改 `engine/hedge_engine.py` 的 maker 优先下单异常分流。
- 更新 `tests/test_maker_first_hedge.py`，并以 `tests/test_hyperliquid_client.py` 锁定真实适配器与 SDK 业务拒绝响应的转换行为。
- 不修改 Hyperliquid 报价语义、其他适配器、网格引擎、风控阈值、部署配置或依赖。
