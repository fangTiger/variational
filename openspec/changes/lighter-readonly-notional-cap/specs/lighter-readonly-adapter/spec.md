## ADDED Requirements

### Requirement: 识别 Lighter 业务成功码
系统 MUST 将 Robinhood Chain 实例返回的业务码 `200` 识别为成功，同时 MUST 保持对业务码 `0`、缺失 `code` 字段或字段值为 `None` 的兼容；其他非成功业务码 MUST 转换为明确异常。

#### Scenario: 真实成功码解析持仓
- **WHEN** 账户详情响应包含 `code=200` 和有效持仓
- **THEN** 客户端正常返回持仓且不抛出 API 错误

#### Scenario: 兼容旧响应
- **WHEN** 成功响应的 `code` 为 `0`、`None` 或完全缺失
- **THEN** 客户端继续解析响应而不抛出 API 错误

### Requirement: 解析并缓存 Lighter 账户
系统 MUST 通过公开的 `accountsByL1Address` 端点解析 L1 地址对应的账户索引，并在客户端实例中缓存；未开户或响应无法解析时 MUST 抛出可读异常。

#### Scenario: 成功解析账户索引
- **WHEN** 地址反查响应包含有效账户索引
- **THEN** 客户端缓存该索引供后续账户查询使用

#### Scenario: 地址未开户
- **WHEN** 地址反查响应包含错误码 `21100`
- **THEN** 客户端抛出明确包含地址未开户含义的异常

### Requirement: 正确读取有符号持仓
系统 MUST 按 `symbol` 匹配仓位，并按 `sign * Decimal(position)` 生成统一的 `signed_size`，同时保留匹配到的原始字典。

#### Scenario: 空头方向
- **WHEN** BTC 仓位的 `sign=-1` 且 `position` 为正数量
- **THEN** 返回的 `signed_size` 为负数

#### Scenario: 多头方向
- **WHEN** BTC 仓位的 `sign=1` 且 `position` 为正数量
- **THEN** 返回的 `signed_size` 为正数

#### Scenario: 零数量条目
- **WHEN** 匹配仓位的 `position="0.00"`
- **THEN** 返回的 `signed_size` 等于零

#### Scenario: 市场条目缺失
- **WHEN** 成功的账户响应中完全没有请求的 `symbol`
- **THEN** 返回该市场的零仓位且不抛异常

### Requirement: 读取失败不得伪装为空仓
系统 MUST 对 HTTP 超时、非成功状态码、非 JSON 响应和畸形持仓字段抛出异常，MUST NOT 将这些失败转换为零仓位。

#### Scenario: HTTP 超时
- **WHEN** 账户请求发生 HTTP 超时
- **THEN** 查询抛出异常

#### Scenario: 服务端失败
- **WHEN** 账户请求返回 5xx
- **THEN** 查询抛出异常

#### Scenario: 非 JSON 响应
- **WHEN** 账户请求成功但响应正文不是 JSON
- **THEN** 查询抛出异常

#### Scenario: 畸形持仓列表
- **WHEN** `positions` 包含非对象元素
- **THEN** 查询抛出异常而不是返回零仓位

#### Scenario: 非零仓位方向非法
- **WHEN** 非零仓位的 `sign` 不是 `1` 或 `-1`
- **THEN** 查询抛出异常而不是返回零仓位

#### Scenario: 持仓数量为负
- **WHEN** `position` 是负数而不是无符号数量
- **THEN** 查询抛出异常

### Requirement: 提供标记价与清算信息
系统 MUST 从 `orderBookDetails` 中按市场匹配 `mark_price`，将其作为统一行情的买卖价；有仓时 MUST 返回标记价与清算价，无仓或清算价为零时 MUST 返回 `None`。

#### Scenario: 读取标记价
- **WHEN** 市场详情包含请求市场的 `mark_price`
- **THEN** 返回的统一行情中间价等于该标记价

#### Scenario: 读取清算信息
- **WHEN** 请求市场存在非零仓位和非零 `liquidation_price`
- **THEN** 返回对应的标记价与清算价

#### Scenario: 无清算价
- **WHEN** 请求市场无仓或 `liquidation_price` 为零
- **THEN** 返回 `None`

### Requirement: 禁止自动交易
系统 MUST 对 `market_order` 和 `close_position` 始终抛出 `NotImplementedError`，并在文档字符串中说明这是人工腿只读的有意设计约束。

#### Scenario: 尝试下单
- **WHEN** 调用 `market_order`
- **THEN** 抛出 `NotImplementedError`

#### Scenario: 尝试平仓
- **WHEN** 调用 `close_position`
- **THEN** 抛出 `NotImplementedError`
