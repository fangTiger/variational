## ADDED Requirements

### Requirement: 统一适配器撤单契约
系统 MUST 在抽象适配器、Extended、Variational 和 Lighter 适配器上公开参数顺序
为 `market, order_id` 的异步撤单契约。Extended 与 Variational MUST 保持原底层
撤单行为不变，网格引擎 MUST 在撤单时传入配置的市场。

#### Scenario: 契约签名一致
- **WHEN** 检查任一受支持适配器的 `cancel_order` 方法签名
- **THEN** 前三个参数依次为 `self`、`market`、`order_id`

#### Scenario: Extended 撤单
- **WHEN** 通过统一接口撤销 Extended 订单
- **THEN** 适配器忽略 `market` 并以原订单号调用既有底层撤单接口

#### Scenario: Variational 撤单
- **WHEN** 通过统一接口撤销 Variational RFQ 订单
- **THEN** 适配器忽略 `market` 并将订单号作为原 `rfq_id` 发送

#### Scenario: 网格引擎撤单
- **WHEN** 网格引擎撤销已记录订单
- **THEN** 调用适配器时同时传入配置市场和订单号

### Requirement: 安全换算 Lighter 整数参数
系统 MUST 使用 `Decimal` 将数量和价格按市场小数位向下换算为整数，MUST 拒绝
浮点、非正值、换算后为零的值，以及显式低于交易所最小单位的数量。

#### Scenario: BTC 数量与价格换算
- **WHEN** 以 5 位数量精度换算 `0.00317`，以 1 位价格精度换算 `63400.0`
- **THEN** 分别得到整数 `317` 和 `634000`

#### Scenario: 子精度输入
- **WHEN** 输入包含精度范围之外的小数
- **THEN** 结果向下截断且不会超过调用方请求值

#### Scenario: 非法或过小输入
- **WHEN** 输入为浮点、非正值、换算后为零或低于最小下单量
- **THEN** 在本地抛出明确异常且不发出交易请求

#### Scenario: 整数侧往返
- **WHEN** 将整数基础数量转换为 `Decimal` 后再按相同精度换回整数
- **THEN** 得到原整数

### Requirement: 持久化客户端订单号并解析交易所订单号
系统 MUST 提供字段名为 `id` 的统一订单引用，MUST 将最后使用的
`client_order_index` 持久化并在重启后继续单调递增，MUST 在文件损坏时停止并
报错而不是从 1 重启。系统 MUST 能从字典或对象形式的活动订单中按
`client_order_index` 找到 `order_index`。

#### Scenario: 首次与重启后分配
- **WHEN** 状态文件不存在时首次分配，并在分配两次后重新创建分配器
- **THEN** 首次编号为 1，后续编号连续且重启后的编号大于重启前最后编号

#### Scenario: 状态文件损坏
- **WHEN** 持久化文件不是有效的预期 JSON
- **THEN** 分配器抛出包含损坏含义的异常且不重置编号

#### Scenario: 解析活动订单
- **WHEN** 活动订单中存在匹配的 `client_order_index`
- **THEN** 返回对应 `order_index`；不存在匹配项时返回 `None`

#### Scenario: 引擎读取订单引用
- **WHEN** 引擎通过 `getattr(result, "id")` 读取下单结果
- **THEN** `OrderRef` 暴露 `.id` 字段
