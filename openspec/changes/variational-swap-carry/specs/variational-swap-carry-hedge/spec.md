## ADDED Requirements

### Requirement: 资金费采样器
系统 SHALL 提供只读采样器，采集同一标的的永续年化费率与 swap 多空年化费率并落盘，
SHALL NOT 触发任何下单动作。采样结果 SHALL 用于积累费率分布，
SHALL NOT 单独作为上实盘的门槛。

#### Scenario: 采集一轮样本
- **WHEN** 采样器对标的对 `(XAU, XAUS)` 执行一轮采集
- **THEN** 记录时间戳、perp 年化费率、swap `long_rate`/`short_rate`、净 carry、
  双腿盘口价与点差
- **AND** 原始值与归一化值同时保存

#### Scenario: 单侧读取失败
- **WHEN** 任一腿的费率或行情读取失败
- **THEN** 记录失败原因并跳过该轮，采样器继续运行

### Requirement: 探针仓验证
系统 SHALL 提供最小名义（约 $5）的探针仓工具，用于在放大前一次性验证
swap 资金费结算记录形态、isolated 实际强平价、多日计提规则、
OI 积分是否对两腿独立计数、开市后能否成交。

#### Scenario: 探针仓开平闭环
- **WHEN** 在同一交易时段内开探针仓并平仓
- **THEN** 记录实际磨损、两腿强平价、成交价与报价偏离
- **AND** 名义不得超过探针仓硬上限

#### Scenario: 探针仓结果未达标
- **WHEN** 任一验证项与预期不符
- **THEN** 阻止进入放大阶段并输出不符项

### Requirement: 同所 delta 中性 carry 对冲
系统 SHALL 支持在同一账户内对同一底层同时持有 swap 腿与永续腿、方向相反、
名义相等的 delta 中性仓位。v1 方向 SHALL 固定为多 XAUS + 空 XAU，
SHALL NOT 自动开反向仓。

#### Scenario: 阈值按扣除摩擦后的收益 R 而非净 carry
- **WHEN** 策略评估是否开仓
- **THEN** 计算 `R = f_perp × 持仓比例 − f_swap × 计息比例 − 往返次数 × 成本`
- **AND** 两腿的比例系数必须分别计算（永续腿连续计提、swap 腿每日离散计提），
  SHALL NOT 对净 carry 统一乘同一个系数
- **AND** 使用 7 日中位 R，而非当前瞬时读数
- **AND** R 低于 `min_R_annual` 或低于 `3 × 年化摩擦` 时拒绝开仓并记录原因

#### Scenario: 政策作为收益模型的输入
- **WHEN** 计算 R
- **THEN** `weekly_flat` 与 `hold_through` 两种政策分别给出各自的 R
- **AND** SHALL NOT 把某一种政策写死进公式

#### Scenario: 退出条件
- **WHEN** 7 日中位 R ≤ 0 并持续 24 小时
- **THEN** 在 swap 可交易时段内平掉两腿

#### Scenario: 入场后的短期滞回
- **WHEN** 入场后不足 `min_commit_cycles` 个周循环且未触发任何豁免条件
- **THEN** 不因 carry 普通回落而平仓
- **AND** SHALL NOT 使用「最短持仓 N 天」形式的规则——周末平仓政策下
  最大持仓仅 4 天 22.5 小时，任何 N ≥ 5 的天数规则永不可达

#### Scenario: 豁免条件无条件优先
- **WHEN** 触发周末平仓、单腿失衡、ADL、kill switch 或地区封锁中的任一项
- **THEN** 立即执行相应处置
- **AND** 一切持仓期约束 MUST 被豁免，不得阻止平仓动作

#### Scenario: 永续腿费率转负快速退出
- **WHEN** 永续腿资金费对我方转为负值并连续 3 个结算周期
- **THEN** 在下一个可交易窗口立即平掉两腿，不受最短持仓期约束

#### Scenario: carry 转负不自动翻向
- **WHEN** 反向组合的净 carry 高于当前方向
- **THEN** 系统只退出，不自动开反向仓

### Requirement: 双腿事务执行
系统 SHALL 以持久化状态机执行双腿开平，状态为
`FLAT → LEG1_SUBMITTED → LEG1_CONFIRMED → LEG2_SUBMITTED → HEDGED → EXITING / INCIDENT`。
下单顺序 SHALL 为先 swap 腿后永续腿。每次下单前 SHALL 写入意图日志与幂等键。

#### Scenario: 正常开仓
- **WHEN** 第一腿（swap）确认成交数量为 Q
- **THEN** 第二腿按 Q 对应等值名义下单
- **AND** 临时净 delta 不得超过硬上限，且必须在配置秒数内恢复

#### Scenario: 第二腿失败且可回滚
- **WHEN** 第二腿下单被明确拒绝，且 swap 仍在可交易时段
- **THEN** 以 `reduce_only` 回滚第一腿并回到 `FLAT`

#### Scenario: 回滚失败
- **WHEN** 第一腿的 `reduce_only` 回滚也失败
- **THEN** 进入 `INCIDENT`，停止全部新开仓，发最高级别告警，等待人工介入

#### Scenario: 下单结果未知
- **WHEN** `/quotes/accept` 超时或连接中断，无法确定是否成交
- **THEN** SHALL NOT 重试下单
- **AND** 轮询 `/positions` 与 `/transfers` 交叉对账后再决定
- **AND** 判定前容忍 `/positions` 的最终一致延迟

#### Scenario: 进程在第一腿成交后崩溃
- **WHEN** 进程重启且持久化状态为 `LEG1_CONFIRMED`
- **THEN** 从持久化状态与交易所实仓对账恢复
- **AND** 恢复前不得发起任何新开仓

#### Scenario: 余额被其他策略占用
- **WHEN** 余额检查通过但下单时可用保证金已被其他策略占用
- **THEN** 按「第二腿失败」路径处理，不得重试下单

#### Scenario: 会话失效
- **WHEN** 请求返回会话过期或 Cloudflare 拦截
- **THEN** 区分「会话失效」与「地区封锁」并分别告警
- **AND** 不得把会话失效误判为下单被拒而重试

#### Scenario: 地区封锁
- **WHEN** `/quotes/accept` 返回 403 restricted jurisdiction
- **THEN** 判定为确定拒绝，不进入未知态
- **AND** 告警提示需在放行 IP 上执行

#### Scenario: 单写者约束
- **WHEN** 已有一个策略实例持有写锁
- **THEN** 第二个实例拒绝启动

### Requirement: 休市窗口风控
系统 SHALL 在 swap 不可交易期间禁止新开仓，SHALL 在临近休市时冻结开仓，
且 v1 SHALL 在周末长休市前平掉两腿。

#### Scenario: pre-close 冻结窗口
- **WHEN** 距 `trading_schedule.next_close_at` 不足 `pre_close_freeze`（默认 ≥30 分钟）
- **THEN** 拒绝任何开仓与加仓
- **AND** 理由是第二腿失败后将无法在休市前回滚第一腿

#### Scenario: 休市期间禁止开仓
- **WHEN** swap 腿当前不可交易
- **THEN** 拒绝任何开仓请求并记录原因
- **AND** 不因此关闭对既有仓位的监控

#### Scenario: 周末前平仓
- **WHEN** 距周末长休市不足配置时长且仍持有仓位
- **THEN** 平掉两腿
- **AND** 平仓失败时告警并进入 `INCIDENT`

#### Scenario: 休市期间数据不可得不得触发平仓
- **WHEN** swap 腿因休市而无法取得报价
- **THEN** 系统判定为正常状态
- **AND** SHALL NOT 触发「数据不可得即平掉两腿」的风控动作

### Requirement: 单腿失衡处置
系统 SHALL 检测只剩一条腿的状态并立即处置。

#### Scenario: 单腿被强平或 ADL
- **WHEN** 检测到一条腿已消失而另一条腿仍存在
- **THEN** 在可交易时段内立即平掉剩余腿
- **AND** 告警并进入停机态，禁止自动重建仓位

#### Scenario: 永续腿可平而 swap 腿休市
- **WHEN** swap 腿在休市期间被强平，永续腿仍存在
- **THEN** 立即平掉永续腿（其 24/7 可交易）并告警

#### Scenario: 裸仓判定容忍读仓延迟
- **WHEN** 刚下过单且 `/positions` 尚未反映
- **THEN** 轮询容忍最终一致延迟后再判定
- **AND** 不得据即时读仓判定为裸仓

### Requirement: 开市后报价健康检查
系统 SHALL 在 swap 开市后确认报价可用，再恢复自动交易。

#### Scenario: 开市后报价异常
- **WHEN** 开市后 N 分钟内取不到可成交报价，或点差超过阈值
- **THEN** 不交易、告警、标记为需人工处置

#### Scenario: 市场被切为只减仓
- **WHEN** 标的 `is_close_only_mode` 为真
- **THEN** 禁止开仓，只允许减仓，并告警

### Requirement: 名义上限与紧急停止
系统 SHALL 设置硬性名义上限与 kill switch。

#### Scenario: 超出名义上限
- **WHEN** 开仓将使任一腿名义超过 `max_notional`
- **THEN** 拒绝开仓

#### Scenario: 触发 kill switch
- **WHEN** kill switch 被激活
- **THEN** 停止全部新开仓，并在可交易时段内平掉现有仓位

### Requirement: Carry 对冲监控
系统 SHALL 输出运行快照，包含两腿名义、净 delta、净年化 carry、
累计已结算资金费、两腿保证金率与强平距离、swap 交易时段状态。

#### Scenario: 输出一次快照
- **WHEN** 监控周期到达
- **THEN** 输出上述全部字段
- **AND** 净 carry 按年化小数口径计算

#### Scenario: 资金费台账来源
- **WHEN** 计算累计已结算资金费
- **THEN** 使用 `position.cum_funding` 与 `/transfers` 对账
- **AND** SHALL NOT 用费率乘名义自行重算

#### Scenario: 强平距离告警
- **WHEN** 任一腿的强平距离低于配置阈值
- **THEN** 触发告警
- **AND** isolated 腿按其自身桶计算，不计入账户其余权益

#### Scenario: 告警送达验证
- **WHEN** 部署或定期自检时
- **THEN** v1 验证 macOS 本地通知能够调用，而非仅写入普通日志
- **AND** 当前没有远程告警，安全处置 SHALL NOT 等待人工响应

### Requirement: 完整周循环无人值守守护进程
系统 SHALL 提供每五分钟运行一轮的守护进程。守护进程 SHALL 先执行全部
平仓与风控检查，命中任一检查后结束本轮；否则按 XAUS 的权威交易时段在
`XAUS_XAU`（多 XAUS + 空 XAU）与 `XAU_XAUT`（多 XAU + 空 XAUT）之间选择目标结构。
实际结构不符时 SHALL 先平旧结构、确认三个受管标的均无旧仓，再开目标结构。
系统 SHALL NOT 自动补腿，且 SHALL NOT 让两个结构同时持仓。
当安全信息不完整时 SHALL 按失败关闭原则拒绝开仓，既有仓位继续按失败关闭原则退出。

#### Scenario: 固定优先级自动处置
- **WHEN** kill switch、单腿或名义失衡、XAUS 强平距离不足、长休市临近中任一条件命中
- **THEN** 守护进程按上述顺序执行首个命中的处置并结束本轮
- **AND** kill switch 同时阻止人工 `open`

#### Scenario: 权威强平价不可用
- **WHEN** 已持有 XAUS 且 `get_liquidation_info` 未返回有效权威强平价
- **THEN** 不得假设安全，立即尝试清空两腿

#### Scenario: 区分每日休市和长休市
- **WHEN** `closure_duration > 4 小时` 且距休市不超过 `SWITCH_LEAD_TIME`（默认 60 分钟）
- **THEN** 从 `XAUS_XAU` 切换至 `XAU_XAUT`
- **WHEN** `closure_duration <= 4 小时`
- **THEN** 持有当前结构穿过每日短休市，不因该窗口反复切换

#### Scenario: 长休市结束后恢复工作日结构
- **WHEN** XAUS 已恢复可交易且目标结构各腿费率有效
- **THEN** 从 `XAU_XAUT` 切换至 `XAUS_XAU`
- **WHEN** XAUS 刚开市但费率尚不可用
- **THEN** 本轮保留 `XAU_XAUT`，下一轮重新判断

#### Scenario: 原子切换
- **WHEN** 实际结构与目标结构不符
- **THEN** 先以 `reduce_only` 平旧结构，并在确认全平后才开目标结构
- **AND** 平仓失败时不打开目标结构并记录、告警
- **AND** 平仓成功但开仓失败时保持空仓，记录失败并在后续轮次重试

#### Scenario: 交易时段元数据不可信
- **WHEN** 时段元数据缺失、畸形、陈旧或缺少完整休市长度
- **THEN** 不执行结构切换；若现有结构含 XAUS，则按不确定处理并尝试清空两腿
- **AND** 不得把陈旧元数据里的不可交易结果当作权威休市状态而跳过 XAUS 平仓尝试

#### Scenario: XAUS 权威确认休市
- **WHEN** 新鲜元数据确认 XAUS 休市且 XAUS 腿无法平仓
- **THEN** 先平掉仍可交易的 XAU 腿（若存在）
- **AND** 持久化 `pending_xaus_close`，不得报告全部平仓成功

#### Scenario: 下单能力失效
- **WHEN** 平仓遇到地区封锁、会话失效或有限次数重试耗尽
- **THEN** 写入显著事故状态、累计连续失败数、弹 macOS 本地通知并非零退出

#### Scenario: 自动开仓前置条件
- **WHEN** 两腿均为空仓
- **THEN** 对含 XAUS 的目标结构，仅在 XAUS 当前可交易、距下次休市超过 2 小时、净 carry 不低于 5% 年化、
  可用保证金足够、kill switch 不存在且当日尝试未达上限时尝试开仓
- **AND** 任一条件缺失或不可读时跳过并记录明确原因
- **AND** 每腿默认名义为 $2,000，可由命令行或 `AUTO_OPEN_NOTIONAL_USD` 覆盖，
  且不得超过执行器的 $3,000 硬上限

#### Scenario: 复用双腿执行与回滚
- **WHEN** 自动开仓全部前置条件满足
- **THEN** 守护进程直接复用 `hedge_swap_carry.cmd_open` 的名义配平、成交回读、
  第二腿执行与 `reduce_only` 回滚能力
- **AND** SHALL NOT 在守护进程中实现另一套双腿成交逻辑

#### Scenario: 偏斜业务拒绝
- **WHEN** 开仓返回 message 含 `skew` 的 HTTP 422
- **THEN** 记录心跳与审计，结束本轮并允许下一轮重试
- **AND** 不弹通知、不累计为守护故障
- **AND** 其他不含 `skew` 的 HTTP 422 仍按故障处理

#### Scenario: 每日尝试上限
- **WHEN** 当日自动开仓尝试达到 20 次
- **THEN** 当日剩余轮次不再读取目标 carry、询价、开仓或切换结构

#### Scenario: 自动开仓回滚失败
- **WHEN** 第二腿失败且第一腿 `reduce_only` 回滚也失败
- **THEN** 持久化 `INCIDENT` 并弹最高级别通知
- **AND** 后续轮次即使实仓已归零也不得自动开仓，直到人工解除状态

#### Scenario: 单独关闭自动开仓
- **WHEN** 使用 `--no-auto-open`
- **THEN** 跳过全部自动开仓动作
- **AND** 保留 kill switch、失衡、强平距离和长休市等自动平仓能力

#### Scenario: 单独关闭自动切换
- **WHEN** 使用 `--no-auto-switch`
- **THEN** 不因黄金现货时段切换结构
- **AND** 保留 kill switch、失衡、强平距离、账户保证金率等既有风控

#### Scenario: pre-close 冻结按目标结构生效
- **WHEN** 目标结构为不含 XAUS 的 `XAU_XAUT`
- **THEN** XAUS 的 pre-close 开仓冻结不得阻挡目标结构开仓

#### Scenario: 每轮心跳与审计
- **WHEN** 守护进程完成任意一轮，包括无动作、dry-run 或失败轮次
- **THEN** 原子写入包含时间、结论、两腿名义、净 delta、XAUS 时段和连续失败数的心跳
- **AND** 心跳记录本轮是否尝试开仓、开仓结论与当日已尝试次数
- **AND** 追加 JSONL 审计记录
- **AND** `hedge_swap_carry status` 置顶显示心跳年龄，超过 15 分钟时标红

#### Scenario: dry-run
- **WHEN** 使用 `--dry-run`
- **THEN** 完成同样的读取和判定并打印计划动作
- **AND** 绝不调用报价接受接口，不得把计划动作记录成真实开仓或平仓
