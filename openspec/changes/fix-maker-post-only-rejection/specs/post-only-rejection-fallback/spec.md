## ADDED Requirements

### Requirement: post-only 立即成交拒绝必须安全降级

系统 MUST 将 maker 挂单阶段的 post-only 立即成交类业务拒绝视为可预期竞态，不得仅因该拒绝让整轮失败。识别 MUST 大小写不敏感，并兼容包含 `post only`、`post-only`、`immediately match` 及等价立即执行语义的不同交易所措辞。

#### Scenario: Hyperliquid 原始拒绝文案

- **WHEN** post-only 下单返回 `Post only order would have immediately matched` 类错误
- **THEN** 系统不向上抛出该错误，并进入真实仓位重读与市价补差路径

#### Scenario: 不同大小写和连接符

- **WHEN** 交易所使用大小写或连接符不同的 post-only 拒绝措辞
- **THEN** 系统仍识别为可预期竞态并执行相同降级

#### Scenario: 无关下单错误

- **WHEN** 下单失败原因为余额、权限、精度、连接或其他非 post-only 立即成交错误
- **THEN** 系统沿既有失败路径向上抛出，不得发送市价补单

### Requirement: 降级补单必须以实际仓位差为准

系统 MUST 在 maker 挂单前保存真实仓位，并在 post-only 拒绝后重新读取真实仓位。补单变化量 MUST 等于目标变化量减去两次读取之间的实际仓位变化量，不得直接使用原 maker 委托量。

#### Scenario: 拒绝期间仓位已部分变化

- **WHEN** 原目标变化量为卖出 1，拒绝后重读显示真实仓位已经减少 0.4
- **THEN** 系统只市价卖出 0.6，最终仓位变化量收敛到卖出 1

#### Scenario: 实际仓位已覆盖目标

- **WHEN** 拒绝后重读的实际仓位变化已经覆盖目标变化量
- **THEN** 系统不再发送市价单

#### Scenario: 拒绝后仓位不可读

- **WHEN** 系统无法可靠重读拒绝后的真实仓位
- **THEN** 系统保守失败，不得按原委托量盲目发送市价单

### Requirement: 正常 maker 执行行为必须保持不变

系统 MUST 保持既有 maker 全成、部分成交、超时撤单、撤单竞态与订单查询降级行为不变。

#### Scenario: maker 正常全部成交

- **WHEN** post-only 挂单成功且订单状态显示全部成交
- **THEN** 系统不发送 taker 单，并报告 maker 全部成交
