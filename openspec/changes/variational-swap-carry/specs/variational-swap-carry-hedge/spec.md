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

### Requirement: Variational 会话到期预警
系统 SHALL 从当前会话 Cookie 的 `vr-token` JWT payload 只读解析 `exp`，
SHALL NOT 校验签名或输出 token 值。无后缀 token 优先；仅存在钱包后缀 token 时，
系统 SHALL 只读取与当前钱包地址匹配的值。解析失败 SHALL 降级为无数据而不抛异常。

#### Scenario: 会话进入 24 小时预警窗口
- **WHEN** 会话剩余时间小于 24 小时且不少于 6 小时
- **THEN** 守护进程把 UTC 到期时间和剩余小时写入心跳与审计
- **AND** 弹 macOS 本地通知，同类通知每两小时最多一次

#### Scenario: 会话进入 6 小时严重窗口
- **WHEN** 会话剩余时间小于 6 小时
- **THEN** 守护进程每轮弹 macOS 本地通知
- **AND** 审计记录标记为 `critical`

#### Scenario: 会话已经过期
- **WHEN** JWT 的 `exp` 不晚于当前时间
- **THEN** 守护进程拒绝自动开仓与结构切换，并写明「会话已过期，需人工刷新 Cookie」
- **AND** 不得仅因会话过期触发平仓或调用交易 API

#### Scenario: 面板显示剩余时间
- **WHEN** 面板读取守护心跳
- **THEN** 显示会话剩余小时，并按 `>48h`、`24~48h`、`6~24h`、`<6h` 分别使用
  `good`、`normal`、`warn`、`bad` 色调
- **AND** 小于 6 小时或已过期时产出 `critical` 告警和 Cookie 重新导出指引
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

### Requirement: 面板按守护心跳解释当前结构
面板 SHALL 只使用守护进程心跳的合法 `structure` 字段解释 carry 持仓，
SHALL NOT 使用模块默认结构猜测腿方向或判定缺腿。面板 SHALL 从 `/positions`
读取全账户持仓，并忽略属于 timed_volume 策略的 BTC 持仓对 carry 告警的影响。

#### Scenario: 心跳结构合法
- **WHEN** 心跳的 `structure` 是已定义的 carry 结构
- **THEN** 面板按该结构标注多空腿并判定缺腿裸仓
- **AND** 不得因旧默认结构与真实结构不同而产生缺腿告警

#### Scenario: 心跳结构不可用
- **WHEN** 心跳缺失、没有 `structure` 字段或该字段不是合法结构
- **THEN** 面板显示「结构未知（守护心跳不可用）」并按 `/positions` 原样列出全部实际持仓
- **AND** 只产出 warning 级结构告警，动作指引为检查守护进程是否在运行
- **AND** 不得猜测腿方向或产出缺腿裸仓告警

#### Scenario: 存在结构外残留腿
- **WHEN** 合法心跳结构之外仍有非零、非 BTC 的账户持仓
- **THEN** 面板展示该实际持仓并产出 critical 级残留腿告警
- **AND** BTC 持仓可以展示，但不得触发 carry 结构告警

#### Scenario: dry-run
- **WHEN** 使用 `--dry-run`
- **THEN** 完成同样的读取和判定并打印计划动作
- **AND** 绝不调用报价接受接口，不得把计划动作记录成真实开仓或平仓

### Requirement: 持仓出场费率评估独立于入场目标校验
入场及切换目标 SHALL 继续要求待开结构等于当前时段目标。
已持仓出场 carry SHALL 只以全部腿费率能否成功读取为依据，不受该目标限制；
读取失败保持连续计数，可读且非正则累计，可读且为正则清零。
既有更高优先级平仓风控 SHALL 保持不变。

#### Scenario: 开市后仍持有负 carry 的 XAU_XAUT
- **WHEN** 目标为 XAUS_XAU，持有 XAU_XAUT，且两腿当前费率可读
- **THEN** 旧结构 carry 参与退出计数
- **AND** 连续达到既有阈值轮数时优先平仓，不进入切换开仓

#### Scenario: 持仓费率读取失败
- **WHEN** API 报错或缺少必要字段且无可用降级
- **THEN** 出场计数不增加也不清零，并记录读取失败

### Requirement: XAUS 资金费自动对账
守护程序 SHALL 每轮只读分页拉取真实 `/transfers`，按
`reference_instrument` 识别 XAUS swap，使用 `qty` 和
`ref_instrument_position_qty`，将新结算去重追加到可注入的 JSONL。
对账 SHALL 保留原始流水、created_at、apply_time 及来源、结算仓位数量、
独立价格来源及名义、实际单次费率、结算前最近一次对应 long_rate、
实际/预测日费率比值、bp 差、上次计提间隔与推断覆盖天数。
缺少流水计提时间时只允许匹配结算前采样的本期预测时间，并标注来源；
缺少流水价格时使用两小时内的采样标记价或 RFQ 中价，必须标注为估计。
不得使用扣款除以流水费率构造名义后再声称独立对账成功。

#### Scenario: 普通日、周五预收与周一追补
- **WHEN** 实际费率与预测年化费率除以 365 的比值接近 1
- **THEN** 输出「日常计提正常」
- **WHEN** 周五覆盖 3 天
- **THEN** 输出「周五预收模式」，列出周五、周六、周日
- **WHEN** 周一覆盖 3 天且与上次 apply_time 间隔至少 3 天
- **THEN** 输出「周一追补模式」，列出周六、周日、周一
- **AND** 上述日期属于推断结果，估计名义不能当成精确结算价格证据

#### Scenario: 缺证据与偏差告警
- **WHEN** 无对应结算前预测
- **THEN** 输出「无预测可比」，不虚构费率、覆盖天数或 apply_time
- **WHEN** 实际与预期单日或三日费率相对偏差超过可配置阈值（默认 20%）
- **THEN** warning 告警「预测口径可能有问题」，同时保留未经三日调整的比值和 bp 差
- **AND** 心跳与审计展示结论，只读 CLI 支持 `--last N`

### Requirement: 补仓分类限流
常规维护 SHALL 每日最多 30 次，成功回读达标后才计入常规次数，
距离上次成功不足 15 分钟跳过且不增加异常次数。
POST 失败、回读不达标及计算异常 SHALL 计入独立异常计数，每日最多 5 次。
请求前 SHALL 持久化预占异常额度；成功回读后转为常规额度，防止中断绕过限流。
旧混合 attempts SHALL 保留并计为常规用量，不能凭空推断历史异常或成功时间。

#### Scenario: 分类触顶及单轮去重
- **WHEN** 异常次数达到 5
- **THEN** 停止补仓并记 critical
- **WHEN** 常规次数达到 30
- **THEN** 停止补仓并记 warning
- **WHEN** 补仓成功且回读距离达标
- **THEN** 同轮不再 POST，心跳分别展示两类计数
- **AND** dry-run 不发送 POST，也不消耗真实补仓计数

### Requirement: 实际隔离桶与危险窗口口径
守护及面板 SHALL 从实际强平距离反推当前桶：`distance * (notional - maintenance_margin) + maintenance_margin`。
`initial_margin` SHALL 仅标注为公式要求值、不含 allocation 追加部分，不能用于实际桶或达标判定。

#### Scenario: IM 不变但实际保护已到位
- **WHEN** 做多距离为 `(mark - liquidation) / mark`，做空距离为 `(liquidation - mark) / mark`
- **THEN** 以该实际距离判断是否达到补仓目标；已达标不 POST，补仓后回读达标记成功且不增加异常计数
- **AND** 名义 1989.51、MM 99.48、标记价 4399.23、做多强平价 4072.19 时当前桶约为 239.98

#### Scenario: 危险窗口与补仓目标分离
- **WHEN** 实际距离为 7.4%，其他风险与时间条件正常且异常计数为零
- **THEN** 使用 normal 轮询，未满普通间隔可零网络早退
- **WHEN** 距离低于 5% 或补仓异常计数大于零
- **THEN** 使用 critical 轮询；5% 表示已经逼近危险，而非未达 8% 最优目标

### Requirement: 补仓台账迁移与显式重置
守护 SHALL 启动时将旧 `swap_carry_guard_state.json.allocation.json` 兼容迁移到 `swap_carry_guard_allocation_state.json`，保留状态；新文件存在时不得覆盖。

#### Scenario: 迁移及恢复异常额度
- **WHEN** 只有旧台账存在
- **THEN** 持有新旧锁后原子迁移，旧锁被占用时拒绝迁移
- **WHEN** 显式使用 `--reset-allocation-counters`
- **THEN** 仅清零异常计数，保留常规用量、总次数、成功时间及重置审计记录，退出且不创建交易所客户端
- **AND** dry-run 不清零，不发送 POST；占锁时拒绝实际重置

### Requirement: 异步补仓转换确认
`set_isolated_allocation` SHALL 返回响应中的 `conversion_id`，守护 SHALL 先轮询
`GET /sub_accounts/conversions/{conversion_id}`，转换为 `confirmed` 后才回读仓位校验距离。
轮询间隔由 `ALLOCATION_CONVERSION_POLL_INTERVAL_SECONDS` 配置，默认 2 秒；
守护等待总超时由 `ALLOCATION_CONVERSION_TIMEOUT_SECONDS` 配置，默认 30 秒，覆盖请求与休眠。

#### Scenario: 确认、拒绝与超时分别计数
- **WHEN** 转换为 `confirmed` 且有限回读内实际距离达标
- **THEN** 记成功并将预占异常额度转为常规额度，容忍确认后首次仓位仍旧
- **WHEN** 转换为 `rejected`
- **THEN** 记为 POST 失败，消耗异常额度，本轮不再发送 POST
- **WHEN** 转换确认超时
- **THEN** 记 warning、释放预占异常额度，不回读校验且本轮不重试
- **AND** dry-run 不 POST、不等待转换

### Requirement: 关键轮询原因可诊断
心跳 SHALL 始终记录 `critical_reasons` 列表，由当前快照汇总状态、事件、强平距离、
异常计数、账户保证金率及切换窗口等触发条件。正常窗口 SHALL 为空列表。
本轮入口的旧 `polling_mode` SHALL NOT 覆盖本轮完成后的最新风险判断。

#### Scenario: 从关键轮次恢复正常
- **WHEN** 以 critical 启动但本轮确认安全，距离 7.4%、无 incident、保证金率高于 3.0x、距切换很远且无其他风险
- **THEN** 心跳记录 normal 与空原因列表；下一次距完整轮次不足 300 秒时可早退
- **WHEN** 隔离腿距离为 4.5%
- **THEN** 记录 critical，原因包含该腿的 `liquidation_distance` 项
