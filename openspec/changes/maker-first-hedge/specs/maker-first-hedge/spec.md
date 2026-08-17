## ADDED Requirements

### Requirement: maker 优先执行必须精确覆盖目标变化量
系统 MUST 根据目标变化量方向挂 post-only 限价单：SELL 使用 best ask，BUY 使用 best bid。系统 MUST 使用 `get_order_by_id(market, order_id)` 返回的订单状态与 `filled_qty` 判断成交，不得使用持仓差值。超时后系统 MUST 尝试撤单，并且无论撤单成功或失败都 MUST 再读取一次订单；若仍有剩余量，只能按 `amount - filled_qty` 吃单。

#### Scenario: maker 全部成交
- **WHEN** 订单状态显示 maker 单已全部成交
- **THEN** 系统不发送 taker 单，并报告全部数量已覆盖

#### Scenario: maker 部分成交后超时
- **WHEN** maker 单只成交部分数量并到达等待超时
- **THEN** 系统撤单、重读订单，并仅对未成交剩余量发送 taker 单

#### Scenario: 撤单失败时出现追加成交
- **WHEN** 撤单失败且重读显示订单又成交了一部分或已全部成交
- **THEN** 系统按重读后的剩余量决定 taker 数量，已全部成交时不得发送 taker 单

#### Scenario: 零变化量
- **WHEN** 目标变化量为零
- **THEN** 系统不读取盘口、不挂单也不吃单

### Requirement: 引擎接入默认保持既有行为
系统 MUST 提供默认值为 `0.0` 的 `maker_first_timeout_s` 配置。配置不大于零时，`HedgeEngine._rebalance` MUST 继续调用既有 `hedge()` 路径；只有显式配置正数时才调用 maker 优先路径。maker 路径 MUST 保留既有再平衡的方向、数量和 `reduce_only` 语义，不得改变再平衡触发条件或任何更早的安全门禁。

#### Scenario: 默认配置
- **WHEN** 调用方没有显式设置 maker 等待时间
- **THEN** 对冲引擎继续直接使用既有 IOC 吃单行为

#### Scenario: 显式启用
- **WHEN** maker 等待时间为正数且本轮已进入再平衡
- **THEN** 引擎按当前 hedge 仓位计算变化量并调用 maker 优先执行

#### Scenario: 同方向减仓
- **WHEN** maker 模式下目标仓位是当前仓位的同方向缩小
- **THEN** maker 单和超时补单都保留 `reduce_only=True`

### Requirement: CLI 必须显式选择并展示 maker 模式
CLI MUST 提供默认值为 `0.0` 的 `--maker-first-timeout` 参数，将其传入 `HedgeConfig`，并在启动摘要中展示该值。系统 MUST NOT 自动修改部署配置或重启运行中的进程。

#### Scenario: 未提供参数
- **WHEN** 使用默认参数启动 CLI
- **THEN** 启动摘要显示 maker 优先关闭且引擎仍走既有 IOC 路径

#### Scenario: 提供正数参数
- **WHEN** 用户显式提供正数等待时间
- **THEN** 该值传入引擎配置且启动摘要显示 maker 优先等待秒数
