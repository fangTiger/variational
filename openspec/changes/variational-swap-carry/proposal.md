# Change: 接入 Variational Swaps 并新增同所 delta 中性 carry 对冲

## Why

Variational 于 2026-08-31 ~ 09-03 上线 swap 类工具（XAUS/XAGS/US100S/US500S），
swap 腿 24h 成交量已是对应永续的 4~17 倍，且资金费改用 TradFi 融资口径
（多头付 ~5.7%/年、空头收 ~2.5%/年），与永续的供需型资金费互相独立。
由此在同一账户内出现可捕获的同标的 carry 价差：实测多 XAUS + 空 XAU
瞬时净 carry ≈ +13.15%/年，delta 中性、零手续费、双腿往返约 9bp。

同时，现有代码**无法交易 swap**（instrument 构造缺 `kind`、`funding_interval_s`
必须为 0）、**读不到 swap 资金费**（需新端点 `/funding/swap`），且
`tracking/monitor.py` 的资金费单位假设经全量结算记录复核被证伪。

**收益必须诚实表述**：+13.15% 是瞬时年化名义收益率，不是预期收益也不是 ROE。
两腿 1x 备资金时毛 ROE ≈ 6.6%。收益支点（XAU 永续费率）半天内可动 1.2 个百分点，
账户历史显示黄金类永续年化费率经常深度转负（PAXG 曾达 -35.7%）。
在当前账户规模下绝对收益是每天 $1 量级，而结构自带 49 小时不可交易窗口、
isolated 2.56% 强平线、单一 OLP 对手方、上线仅 4 天。
**本变更据此采取"取证优先、周末不持仓、探针仓验证"的保守路线。**

## What Changes

### 数据/适配层
- **BREAKING**：修正 `predicted_funding_rate` 单位口径为**年化小数**
  （原假设「百分比 / 结算周期」被全量 535 条真实结算记录证伪，差约 10.95 倍）。
- **BREAKING**：同步校准 Extended 侧资金费单位。按新口径重算，
  **现有 BTC 结构的推荐对冲方向会翻转**，影响正在运行的实盘。
- **BREAKING**：`get_position` 对本结构强制 `exact=True`；
  停止用 `get_liquidation_info` 自算强平价，改读 API 返回值。
- `VariationalClient` 支持 `instrument_type = "swap"`：`funding_interval_s` 固定 0、
  强制携带 `kind`；`_instrument` 的 `funding_interval_s` 默认值改为哨兵 `None`。
- 新增 `/funding/swap` 与 `/settlement_pools/leverage` 封装。
- 从 `supported_assets` 读取 swap 交易时段，对外暴露可交易状态与休市窗口。

### 策略层（新能力）
- 新增只读资金费采样器（用于积累费率分布，非上钱门槛）。
- 新增**探针仓工具**：$5 级最小仓位，一次性验证结算形态、真实强平价、
  多日计提规则、OI 积分计数、开市可成交性。
- 新增同所 swap↔perp delta 中性 carry 对冲策略（v1 方向写死为多 XAUS + 空 XAU），
  带**持久化执行状态机**与「结果未知」对账路径。
- **休市风控**：v1 周五休市前平掉两腿；新增 pre-close 冻结窗口，
  距休市不足 N 分钟禁止开仓（防第二腿失败后无法回滚而留下 49 小时裸仓）。
- 单腿被强平 / ADL 后立即平掉另一腿并进入停机态。

## Impact

- Affected specs: `variational-swap-market`（新增）、`variational-swap-carry-hedge`（新增）
- Affected code: `adapters/variational_client.py`、`tracking/monitor.py`、
  `tools/hedge.py`、`tracking/track_equity_util.py`、新增策略与工具模块
- Affected tests: 新增适配层与策略测试；所有风控场景要求先写失败测试
- **影响在跑的实盘**：Extended 单位校准会翻转现有 BTC 结构的方向建议
- 不复用也不修改：`engine/hedge_engine.py`、`engine/risk.py`（见 design D4）
