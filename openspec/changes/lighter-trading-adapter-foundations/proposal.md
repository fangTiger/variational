## Why

Lighter 撤单必须同时提供市场索引和交易所订单号，而现有适配器的
`cancel_order` 契约只接收订单号；同时 Lighter 下单参数使用整数精度，且下单
响应不返回撤单所需的 `order_index`。若不先统一这些基础能力，跨适配器调用会
触发签名错误，精度错误可能放大仓位，进程重启后的客户端订单号复用还可能导致
撤错订单。

## What Changes

- 将适配器撤单契约统一为 `cancel_order(market, order_id)`，Extended 与
  Variational 忽略冗余的 `market`，网格引擎调用点同步传入市场。
- 新增只接受 `Decimal` 的 Lighter 数量与价格整数换算，并校验正数、最小精度和
  交易所最小下单量。
- 新增统一的 `OrderRef`，以及跨重启持久化的 `client_order_index` 分配器和
  `client_order_index -> order_index` 查询函数。
- 所有行为先由无真实网络的单元测试固定，再实现最小代码。

## Capabilities

### New Capabilities

- `lighter-trading-adapter-foundations`: 提供 Lighter 交易适配器所需的统一撤单契约、
  精度换算和订单号映射基础层。

### Modified Capabilities

无。

## Impact

- 修改 `adapters/base.py`、`adapters/extended_client.py`、
  `adapters/variational_client.py` 和 `grid/grid_engine.py`；其中引擎只修改唯一的
  撤单调用行。
- 新增 `adapters/lighter_scale.py`、`adapters/order_ref.py` 及三个对应测试文件。
- 必要时同步旧契约的测试假对象签名。
- 不涉及 Lighter 交易开关、实际挂单/撤单/查单实现、SDK 安装或真实网络验证；
  即原计划 Task 4–6 全部排除。
