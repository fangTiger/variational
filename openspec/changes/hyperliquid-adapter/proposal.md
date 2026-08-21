# Change: Hyperliquid 交易适配器

## Why

当前对冲腿在 Extended。2026-08-22 调研发现 Hyperliquid 更适合承担这个角色：

- **API 成熟度最高**：官方 REST + WebSocket + Python SDK（`hyperliquid-python-sdk`），
  封装 EIP-712 签名与 nonce 管理，远优于现接的两个所。
- **BTC 永续深度最好**，是全球份额最大的永续 DEX 之一。
- **是 Entropy 等第三方前端的底层**：实测其前端 bundle 直接调用
  `api.hyperliquid.xyz/exchange`、`/info`、`wss://.../ws`，并使用
  `maxBuilderFee` —— 即 Hyperliquid 的 Builder Code 机制。因此接入
  Hyperliquid 后，未来若要「通过 Entropy 交易以获取其积分」，只需在下单时
  附加 builder 参数，无需另接一套 API。
- **HIP-3 生态**：pre-IPO、商品、股指等 builder 部署的市场同样结算在
  Hyperliquid 共享订单簿上，同一套 API 即可触达。

## What Changes

- 新增 `HyperliquidClient`，实现项目统一的 `ExchangeAdapter` 接口。
- 使用官方 SDK；凭据支持 **API 代理钱包（agent wallet）**，避免主钱包私钥落地。
- 默认只读；交易能力须显式开启（与 `LighterClient` 的 `trading_enabled` 一致）。
- 补齐引擎与对冲所需能力：持仓、盘口、标记价、余额、挂单、订单查询、
  限价/市价下单、撤单、最小下单量、价格精度、清算信息。
- 新增依赖 `hyperliquid-python-sdk`。
- 修正统一账户权益口径：`get_balance()` 合计永续
  `marginSummary.accountValue` 与 Spot `USDC` 的 `total`，并保留两部分原始值；
  单侧读取失败时按零降级，双侧均失败或合计非正时保守抛出。

## Capabilities

### New Capabilities

- `hyperliquid-adapter`: 定义 Hyperliquid 适配器的接口契约、只读默认、
  凭据形态、下单语义与失败处理。

### Modified Capabilities

无。不改动现有适配器、引擎与任何风控行为。

## Impact

- 新增 `adapters/hyperliquid_client.py` 与无网络测试。
- 修改 `requirements.txt`：新增 `hyperliquid-python-sdk`。
  ⚠️ 该包会把 `eth-utils` 从 6.0.0 降级到 5.3.1；已验证降级后
  完整测试 **862 passed**、Extended 与 Lighter 适配器导入正常。
- **不修改** plist、不改动风控阈值、不切换现有对冲腿。
  切换与否是后续独立决定。

## 费率对比（供后续决策留档）

| | maker | taker |
|---|---|---|
| Extended（现对冲腿） | 0 | 0.0225% |
| Hyperliquid（基础档 VIP0） | 0.015% | 0.045% |

对冲以吃单为主（实测 maker 成交率仅 10.8%，因对冲方向天然逆势），
故 Hyperliquid 成本约为 Extended 的 2 倍。**本变更只提供能力，
不主张切换**；是否切换应结合积分收益另行判断。
