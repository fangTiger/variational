# Lighter × Robinhood Chain 积分对冲 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 用最低成本吃下 Lighter 在 Robinhood Chain 上的 $11M LIT 积分池。人在 Robinhood Wallet 手动持仓拿 2x 乘数，机器人只读该仓位并在 Extended 独立账户上自动对冲掉方向风险，使组合净敞口 ≈ 0。

**Architecture:** 复用现有 `ExchangeAdapter` 抽象与 `HedgeEngine` 主循环。新增一个**只读**的 Lighter 适配器作为 primary 腿（不下单、不持密钥），Extended 作为 hedge 腿跑在**全新的账户前缀 `X10_HEDGE`** 上，与网格账户 `X10_GRID` 完全隔离。

**Tech Stack:** Python 3.11、httpx（已有依赖）、pytest、launchd。

---

## 一、对冲关系表（谁和谁对冲）

| | **primary 腿（拿积分）** | **hedge 腿（对冲风险）** |
|---|---|---|
| 交易所 | Lighter @ Robinhood Chain | Extended（Starknet） |
| 操作方式 | **人工**，Robinhood Wallet 移动端 | **机器人自动**，跟随 primary |
| 标的 | BTC 永续（market_id=1） | `BTC-USD` |
| 方向 | 单边（按资金费选多/空） | 恒为 primary 的**反向等额** |
| 报价资产 | USDG | USDC |
| 账户 | 你的 RH Wallet 地址 | Extended 新子账户 |
| 环境变量前缀 | 无（只读公开接口，不需密钥） | `X10_HEDGE_` |
| 手续费 | 0 / 0（已实测确认） | maker 0、taker 按 Extended 费率 |
| 为什么这么分 | 2x 乘数只在 RH Wallet 生效，且无 API | 必须自动跟随，人跟不过来 |

**净敞口**：`primary.signed_size + hedge.signed_size ≈ 0`。primary 是人手动改的，hedge 由引擎每轮拉平。

**与现有系统的隔离关系**：

| 系统 | Extended 账户前缀 | 标的 | 状态 |
|---|---|---|---|
| BTC 中性网格 | `X10_GRID` | BTC-USD | 实盘运行中，**不得受本次改动影响** |
| 跨所对冲刷积分（旧） | `X10` | BTC-USD | 已停用 |
| **本计划：RH 积分对冲** | **`X10_HEDGE`（新建）** | BTC-USD | 新增 |

三者用不同 Extended 子账户，持仓与保证金互不可见。**这是硬性要求**：若对冲腿与网格共用账户，`grid_engine` 读持仓时会把对冲仓当成自己的库存，`--max-inv` / `--max-drawdown` / `--hard-stop-dist` 三道风控全部失真。

---

## 二、背景（实施者必读）

**已完成的 API 探测，结论直接可用，不要重新摸索：**

- Robinhood Chain 实例的 API base URL 是 **`https://api.rh.lighter.xyz`**（从 `robinhoodchain.lighter.xyz` 的前端 bundle 里挖出来的，官方文档只写了主网的 `mainnet.zklighter.elliot.ai`）
- **公开只读端点无需任何鉴权**，实测可直接 GET：
  - `GET /api/v1/orderBooks` → 全部市场元数据。BTC `market_id=1`、ETH `market_id=0`、SOL `market_id=3`；实测 `taker_fee` 与 `maker_fee` 均为 `"0.0000"`
  - `GET /api/v1/orderBookDetails` → 含 `mark_price`、`index_price`、`open_interest`、`daily_quote_token_volume`
  - `GET /api/v1/accountsByL1Address?l1_address=0x...` → 钱包地址反查 account index；查不到返回 `{"code":21100,"message":"account not found"}`
  - `GET /api/v1/account?by=index&value=<N>` → 账户详情，含 `positions[]`、`collateral`、`available_balance`
- **持仓字段结构（实测样本）：**
  ```json
  {"market_id":1,"symbol":"BTC","sign":-1,"position":"0.00020",
   "avg_entry_price":"...","position_value":"12.586280",
   "unrealized_pnl":"-0.012500","liquidation_price":"66912.223","allocated_margin":"..."}
  ```
- **方向语义（已交叉验证，不要搞反）：`sign=1` 为多头，`sign=-1` 为空头**。`position` 是**无符号**数量，`position_value` 恒为正。验证方式：某 `sign=-1` 的 BTC 仓 `liquidation_price=66912` 高于当时标记价 `62903`，符合空头特征。因此 `signed_size = sign * Decimal(position)`
- 无持仓的市场也会出现在 `positions[]` 里，`position` 为 `"0.00"`，需要过滤

**代码现状：**

- `adapters/base.py` 定义 `ExchangeAdapter` 抽象与 `Position(market, signed_size, raw)`；`hedge()` / `close_position()` 在基类已通用实现，子类不用重写
- `engine/hedge_engine.py` 的 `HedgeEngine` 已实现「读两腿 → 逼近清算价则双平 → 保证金降险 → 再平衡」。**关键安全设计已就位**：任一腿读取失败就跳过本轮不动仓（`hedge_engine.py:104-125`），避免造成裸仓
- `ExtendedClient.from_env(prefix=...)` 已支持账户前缀隔离（`adapters/extended_client.py:104`）
- 跑测试：`.venv/bin/python -m pytest tests/ -q`，**当前基线 288 passed**
- 所有注释和文档用中文，标识符用英文

**测试纪律：每个 Task 先写会失败的测试，且优先覆盖失败路径。** 测试桩默认返回成功会把风控的 P0 缺陷完整放过——这是本仓库踩过的账。

---

## 三、文件结构

| 文件 | 职责 |
|---|---|
| `adapters/lighter_client.py` | **只读** Lighter 适配器。查行情、查持仓；任何下单方法直接抛异常 |
| `engine/hedge_engine.py` | 修改：解除 Variational 专属耦合；新增 `max_primary_notional` 上限保护 |
| `tools/run_lighter_hedge.py` | CLI 入口，dry_run 默认开启 |
| `tools/alert_check.py` | 修改：接入本策略的连通性与超限告警 |
| `tests/test_lighter_client.py` | 适配器测试，含 sign 方向、零仓过滤、网络失败 |
| `tests/test_hedge_notional_cap.py` | 名义上限保护测试（失败路径优先） |

---

## Task 1: Lighter 只读适配器

**Files:**
- Create: `adapters/lighter_client.py`
- Create: `tests/test_lighter_client.py`

- [ ] **Step 1: 写失败测试**

`tests/test_lighter_client.py` 必须覆盖以下场景，全部用假的 HTTP 响应，**不打真网络**：

1. `sign=-1` 的持仓 → `signed_size` 为**负**（方向搞反等于双倍敞口而非对冲，这是本适配器最致命的错误，必须第一个测）
2. `sign=1` 的持仓 → `signed_size` 为正
3. `positions[]` 里 `position="0.00"` 的条目 → 视为无持仓，`signed_size == 0`
4. 请求的 symbol 在 `positions[]` 中完全不存在 → 返回 `signed_size == 0` 的 `Position`，**不抛异常**
5. HTTP 超时 / 5xx / 返回非 JSON → **抛异常**，绝不返回 `signed_size=0`（静默返回 0 会让引擎误以为对手腿已平仓，进而平掉对冲腿，制造裸仓）
6. `accountsByL1Address` 返回 `code=21100` → 抛出可读的「地址未开户」错误
7. 调用 `market_order()` → 抛 `NotImplementedError`

- [ ] **Step 2: 实现**

要点：
- `LighterClient(l1_address=..., base_url="https://api.rh.lighter.xyz")`
- `connect()` 里用 `accountsByL1Address` 解析出 `account_index` 并缓存；解析不到就报错退出，别静默继续
- `get_position(market)`：拉 `/api/v1/account?by=index&value=<idx>`，在 `positions[]` 里按 `symbol` 匹配，`signed_size = sign * Decimal(position)`，原始 dict 放进 `Position.raw`
- `get_market_price(market)`：从 `orderBookDetails` 取，用 `mark_price` 构造 `MarketPrice`（该端点不返回买一卖一；对冲只需要标记价做名义换算，够用）
- `get_liquidation_info(market)`：返回 `(mark_price, liquidation_price)`，供引擎的双平保护用；`liquidation_price` 为 `"0"` 表示无仓，返回 `None`
- **`market_order` / `close_position` 一律抛 `NotImplementedError`**，并在 docstring 写明：这条腿是人工操作的，机器人只读。这是有意的设计约束，不是待办
- 用 `httpx` 且必须设 timeout（建议 10s）

- [ ] **Step 3: 验证** — `pytest tests/test_lighter_client.py -q` 全绿，且总数仍 ≥ 288 + 新增。

---

## Task 2: 引擎泛化 + 名义上限保护

**Files:**
- Modify: `engine/hedge_engine.py`
- Create: `tests/test_hedge_notional_cap.py`

- [ ] **Step 1: 写失败测试**

`max_primary_notional` 是本设计**特有**的风险点：primary 腿由人手动操作，机器人无条件跟随。若手滑在 RH Wallet 开了远超预期的仓位，引擎会忠实地在 Extended 上跟一个同样大的反向仓，直接打爆保证金。

必须先写这些**失败路径**测试：

1. primary 名义金额 **超过** `max_primary_notional` → 引擎**拒绝再平衡**，`action_taken` 含明确超限说明，且**没有任何下单调用发生**（用 mock 断言 `market_order` 调用次数为 0）
2. 超限时**已有**的对冲仓 → **保持不动**，不许自作主张平掉（平掉会把已有的对冲变成裸仓，比超限更危险）
3. 恰好等于上限 → 允许，边界值不误伤
4. `max_primary_notional=None` → 不启用该保护，行为与现在一致（向后兼容）

- [ ] **Step 2: 实现**

- `HedgeConfig` 新增 `max_primary_notional: Decimal | None = None`
- 在 `run_once()` 的**再平衡之前**插入检查：`abs(p_size) * mark_price > max_primary_notional` 则记录告警、设置 `action_taken`、直接 return，不进入 `_rebalance`
- 解除 Variational 专属耦合：`hedge_engine.py:112` 和 `:271` 两处按类名判断 `VariationalAuthError` 的硬编码。改为在 `HedgeConfig` 上放一个可选的「需要自愈的异常类型」集合，默认空。**不要删掉自愈逻辑本身**——旧的 Variational bot 仍留在仓库里作参考
- Lighter 腿无需 Cookie 自愈（只读公开端点），`_on_auth_error` 传 `None` 即可

- [ ] **Step 3: 验证** — 全量 `pytest tests/ -q`。**基线 288 必须不减少**，若有既有测试因为解耦而失败，是解耦做错了，不是测试该改。

---

## Task 3: CLI 入口与账户配置

**Files:**
- Create: `tools/run_lighter_hedge.py`
- Modify: `.env.example`（加 `X10_HEDGE_*` 五个键的说明，**不要动 `.env`**）
- Modify: `README.md`（在「两套系统」表格里加第三套）

- [ ] **Step 1: 实现入口**

参照 `tools/run_grid.py` 的参数风格：

| 参数 | 默认值 | 说明 |
|---|---|---|
| `--live` | 关 | 不加就是 dry_run，只打印意图 |
| `--account` | `X10_HEDGE` | Extended 账户前缀 |
| `--lighter-address` | 必填 | Robinhood Wallet 地址 |
| `--market` | `BTC` | Lighter 侧 symbol |
| `--hedge-market` | `BTC-USD` | Extended 侧 symbol |
| `--max-primary-notional` | `2000` | 名义上限（USD），Task 2 的保护 |
| `--interval` | `30` | 轮询秒数。primary 是人工低频操作，不需要快 |
| `--rebalance-threshold` | `0.02` | 净 delta 超过单腿名义此比例才动手 |

- 启动时**必须打印**：两腿账户标识、标的、上限、`dry_run` 状态。参照 `run_grid.py:102` 打印账户前缀的做法
- **启动自检**：若 `--account` 解析到的前缀是 `X10_GRID`，直接拒绝启动并报错。网格账户绝不能被这个进程碰
- 日志写 `log/lighter_hedge_{日期}.log`

- [ ] **Step 2: 验证** — dry_run 跑通一轮，确认能读到 primary 持仓、算出正确的目标对冲量、且**没有真实下单**。

---

## Task 4: 监控与告警

**Files:**
- Modify: `tools/alert_check.py`
- Create: launchd plist（参照 README 里的模板）

- [ ] **Step 1: 接入告警**

**连通性告警是本 Task 的重点，不是附赠品。** 8/1–8/3 网格无声停摆 35.5 小时、告警全程静默的教训必须在这里落实：

1. **心跳缺失**：进程超过 `3 × interval` 没写出快照 → 告警
2. **净敞口偏离**：`|net_delta|` 占单腿名义超过阈值且**持续 2 轮以上**未收敛 → 告警（单轮偏离是正常的再平衡中间态，不该报）
3. **名义超限**：Task 2 的保护被触发 → 立即告警，这需要人去 RH Wallet 手动缩仓
4. **primary 读取连续失败**：连续 3 轮读不到 Lighter → 告警。此时引擎按设计会停止动仓，但人必须知道
5. **对冲腿保证金**：Extended 可用保证金率跌破阈值 → 告警

- [ ] **Step 2: 验证** — 手工构造每一种异常状态，确认告警**真的弹出来了**。逐条实测，不要只看代码写了就算数。

---

## Task 5: 灰度上线

- [ ] **Step 1: dry_run 观察** — 至少完整跑 24 小时，期间人为在 RH Wallet 调整一次 primary 仓位，确认引擎正确算出新的目标对冲量。

- [ ] **Step 2: 小额实盘** — Lighter 腿 $500，加 `--live`。确认：
  - Extended 上真的建了反向仓，数量与 primary 匹配
  - 净敞口收敛到阈值内
  - Extended 账户的持仓**没有**出现在网格监控面板里（隔离验证）

- [ ] **Step 3: 观察一个结算周期** — 到周五结算后，核对 Lighter 积分是否真的入账、2x 乘数是否生效。**积分没到账就不要放大仓位**，这是整个方案的价值前提。

- [ ] **Step 4: 放大** — 上述全部确认后再谈加仓，并同步上调 `--max-primary-notional`。

---

## 四、需要人工完成的事项（机器人不做）

| # | 事项 | 时点 |
|---|---|---|
| 1 | 确认所在地区可开 Robinhood Wallet 永续（**前置条件，不过则全案作废**） | 最先 |
| 2 | 桥 USDG 到 Robinhood Chain | Task 5 前 |
| 3 | Extended 开新子账户、生成 API key、填 `X10_HEDGE_*` 到 `.env` | Task 3 前 |
| 4 | 给对冲账户划保证金（建议 ≥ primary 名义的 40%） | Task 5 前 |
| 5 | **在 RH Wallet 里**开 BTC 永续仓（网页版只有 1x，别开错地方） | Task 5 |
| 6 | 提供 RH Wallet 地址给 `--lighter-address` | Task 3 |
| 7 | 每周五结算前不平仓；资金费翻转时考虑换边 | 长期 |

---

## 五、已知风险

| 风险 | 处置 |
|---|---|
| **地区限制** | 中国在 Lighter 积分计划的 18 国限制名单内。积分可能事后被审查撤销——官方文档明写「积分可见不等于最终批准」。这是不可通过技术手段消除的风险 |
| **手动腿被误操作** | Task 2 的 `max_primary_notional` 硬上限 + 告警 |
| **两所基差偏离** | 选 BTC（RH 实例日成交 $96.2M，三个主流币里最深）可最小化。不要为了 OI 份额去做 SOL，其日成交仅 $2.1M |
| **90 天零费到期（约 9/29）** | 到期前重算对冲经济性。Extended 侧 taker 费本来就一直在付 |
| **`api.rh.lighter.xyz` 是从前端 bundle 逆出来的** | 非官方文档承诺的端点，可能变更。适配器要把 base_url 做成可配置参数，且连不上时告警而非静默 |
| **积分是否按市场分配未知** | 可另开一小笔 ETH 仓作份额探针，观察两周对比 BTC 的积分产出 |

---

## 六、参考

- Lighter 积分规则：https://docs.lighter.xyz/points-program/lighter-on-robinhood-chain-points
- 零售计分维度（Volume / OI / Funding / 清算 / PnL，非线性，故意亏损不加分）：https://docs.lighter.xyz/points-program/retail
- Lighter API 文档（主网口径，RH 实例同构）：https://apidocs.lighter.xyz/docs/get-started
- 旧版跨所对冲设计（本计划的架构来源）：`docs/superpowers/specs/2026-07-15-variational-hedge-bot-design.md`
