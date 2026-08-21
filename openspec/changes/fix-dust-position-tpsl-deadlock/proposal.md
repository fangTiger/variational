# Change: 微仓不再阻塞 TPSL 与做市挂单

## Why

实盘 Lighter 做市账户持有 `0.00003 BTC`（3 个整数单位）时，整仓 TPSL
仍按完整持仓量下单。该数量低于市场最小下单量 20 单位，交易所必然拒绝；
`_maintain_tpsl` 随即返回失败，主循环按既有保护禁止本轮新增风险仓。微仓本身
又无法单独平掉，于是每轮重复同一失败，网格永远没有成交机会消化微仓，并伴随
大量无效查询和 429。

同一微仓还会进入硬止损清算信息读取；清算数据缺失或请求失败时反复记录
fail-safe。交易所不接受该数量的任何平仓或保护单，因此两个风控入口都应把它
视为不可交易的零仓，而不是可执行风控的普通仓位。

## What Changes

- 统一适配器公开按市场查询最小下单量的异步接口。
- Extended 从已缓存的市场元数据读取 `min_order_size`。
- Lighter 从 `_load_market_meta` 已缓存的 `min_base_units` 与数量精度换算最小量。
- 网格引擎缓存每个市场的有效最小下单量；查询失败短时缓存失败结果以限制请求，
  但保守地继续既有 TPSL 与硬止损行为。
- `_maintain_tpsl` 遇到绝对持仓量低于已知最小量时跳过挂单并返回成功，以限频
  中文日志说明原因。
- `_check_hard_stop` 对同类微仓按零处理，不查询清算价、不触发 fail-safe。
- 达到或超过最小量的仓位完全沿用现有 TPSL、硬止损和失败关闭行为。

## Capabilities

### New Capabilities

- `dust-position-risk-handling`: 定义市场最小下单量查询、缓存、微仓 TPSL 短路、
  硬止损零仓语义以及查询失败时的保守行为。

### Modified Capabilities

- 无。当前仓库没有已归档基线 spec；本 change 以新增能力记录修复后的行为。

## Impact

- 修改 `adapters/base.py`：公开统一最小下单量查询契约。
- 修改 `adapters/lighter_client.py`：从真实市场元数据返回 Lighter 最小下单量。
- 复用 `adapters/extended_client.py` 的真实最小下单量接口及元数据缓存。
- 修改 `grid/grid_engine.py`：增加最小量缓存和两个微仓风控短路。
- 修改无网络测试，覆盖 Lighter/Extended 真实适配器接口、微仓回放、阈值回归、
  查询失败与缓存。
- 不修改任何风控阈值、网格策略、对冲算法或 plist；不访问真实网络。
