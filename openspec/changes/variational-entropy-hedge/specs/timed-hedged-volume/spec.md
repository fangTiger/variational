## ADDED Requirements

### Requirement: 按执行模型选择下单路径
系统 SHALL 根据适配器声明的执行模型选择下单路径,而不是假定所有交易所都支持限价单。

适配器 SHALL 通过 `execution_model` 声明自身类型:`"orderbook"`(默认)或 `"rfq"`。

#### Scenario: RFQ 适配器不走 maker-first
- **WHEN** 某一腿的适配器声明 `execution_model == "rfq"`
- **THEN** 该腿 SHALL 直接调用 `market_order`,不调用 `place_limit_order`
- **AND** 返回的 `HedgeFillResult.used_taker` SHALL 为 `True`

#### Scenario: 订单簿适配器行为不变
- **WHEN** 适配器未声明 `execution_model` 或声明为 `"orderbook"`
- **THEN** 该腿 SHALL 保持现有 maker-first 行为,超时后转市价

#### Scenario: 两腿执行模型可以不同
- **WHEN** 主腿为 RFQ、对冲腿为订单簿
- **THEN** 两腿 SHALL 仍在同一节拍并发提交
- **AND** 主腿走市价、对冲腿走 maker-first

#### Scenario: 能力检查按模型区分
- **WHEN** 对冲腿声明为 RFQ 模型
- **THEN** 可用性检查 SHALL NOT 因缺少 `place_limit_order` / `get_order_by_id` 而判定不可用
- **AND** SHALL 仍要求 `market_order`、`get_market_price`、`get_min_order_size` 可用

### Requirement: 认证失效自愈
系统 SHALL 在主腿抛出认证失效异常时尝试重建客户端,而不是终止进程。

#### Scenario: Cookie 过期触发重载
- **WHEN** 主腿调用抛出注册的认证异常类型(如 `VariationalAuthError`)
- **THEN** 系统 SHALL 重读 `.env`(`override=True`)并重建主腿客户端
- **AND** 本轮 SHALL 记为跳过,既不开仓也不平仓

#### Scenario: 重载后仍失败
- **WHEN** 重建客户端后认证仍失效
- **THEN** 系统 SHALL 置对冲互锁并停止开新仓
- **AND** SHALL 输出告警,等待人工更新 Cookie

#### Scenario: 认证失效不得留下单边仓
- **WHEN** 一腿已成交、另一腿因认证失效未能提交
- **THEN** 系统 SHALL 回滚已成交的那一腿,遵循既有单边收敛规则

## MODIFIED Requirements

### Requirement: 对冲腿账户隔离
系统 SHALL 保证同时运行的多套定时定量策略不共用同一个交易所账户的同一市场。

启动时 SHALL 校验:若检测到本策略的对冲腿账户地址与另一运行中实例相同,
且市场相同,则拒绝启动。

#### Scenario: 两套策略共用 Hyperliquid 账户
- **WHEN** Variational×Entropy 与 Lighter×Entropy 配置了相同的
  `ACCOUNT_ADDRESS` 且 `hedge_market` 相同
- **THEN** 系统 SHALL 拒绝启动并说明原因
- **AND** SHALL 提示改用独立的环境变量前缀指向第二个账户

#### Scenario: 使用独立账户前缀
- **WHEN** 通过 `--hedge-env-prefix HYPERLIQUID_VAR` 指定第二套凭据
- **THEN** 系统 SHALL 从该前缀读取账户地址、代理私钥与 builder 配置
- **AND** 两套策略的持仓读数 SHALL 互不干扰
