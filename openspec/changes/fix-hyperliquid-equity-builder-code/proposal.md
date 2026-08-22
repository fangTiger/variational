# Change: 修正 Hyperliquid 风控权益并接入 Entropy Builder Code

## Why

Hyperliquid 使用统一账户。持仓后 `marginSummary.accountValue` 是与 Spot USDC
重叠的占用记账，当前把两者相加会高估风控权益。实盘数据表明，按“现在全平后
账上可用资金”口径，应只合计 Spot USDC 与全部永续持仓的未实现盈亏。

同时，Entropy 通过 Hyperliquid Builder Code 归属交易。当前适配器虽然使用的
SDK 已支持 `builder` 参数，但所有下单路径都未传入，因此交易不会归属 Entropy。

## What Changes

- `get_balance()` 的 `equity` 改为 Spot USDC 加全部持仓
  `position.unrealizedPnl` 之和。
- 保留 `perps_account_value`、`spot_usdc_total`，新增
  `unrealized_pnl_total` 诊断字段。
- 永续账户状态与 Spot 账户状态均为必需查询；任一失败或最终权益非正时抛出。
- `HyperliquidClient` 支持可选 builder 地址与费率配置，并在限价单、市价单的
  `Exchange.order(..., builder=...)` 调用中透传。
- 默认不启用 builder；环境变量为 `HYPERLIQUID_BUILDER_ADDRESS` 和
  `HYPERLIQUID_BUILDER_FEE_TENTHS_BPS`。
- 构造阶段拒绝非法地址、非整数费率、负费率及超过永续上限 100（0.1bp 单位）的
  费率；0 合法。

## Capabilities

### New Capabilities

- `hyperliquid-equity-builder-code`: 定义 Hyperliquid 风控权益口径、保守失败
  语义及可选 Builder Code 下单归属。

### Modified Capabilities

无。既有 `hyperliquid-adapter` change 尚未归档为基础 spec，本 change 以独立增量
记录修正后的最终契约。

## Impact

- 修改 `adapters/hyperliquid_client.py` 与 `tests/test_hyperliquid_client.py`。
- 新增 OpenSpec proposal、spec delta 与 tasks。
- 不修改网格/对冲引擎、其他适配器、风控阈值、plist 或运行时配置。
- 不停止或重启正在运行的策略；不访问真实网络。
