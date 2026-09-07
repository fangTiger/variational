# Variational Swap Carry 对冲系统

在**同一个 Variational 账户内**，对同一底层（黄金）持有方向相反、名义相等的两条腿，
净方向敞口恒为零，赚的是两条腿**资金费之差**。

> **拿真钱跑的实盘系统。** delta 中性消除的是方向风险，**不是**资金费风险与基差风险。
> 资金费可以转负，两条腿跟的也不是同一个指数。默认 dry-run，务必先小额观察。

---

## 目录

- [它在做什么](#它在做什么)
- [三个标的与两个结构](#三个标的与两个结构)
- [为什么要按时段切换](#为什么要按时段切换)
- [五分钟跑起来](#五分钟跑起来)
- [守护进程做的事](#守护进程做的事)
- [日常运维](#日常运维)
- [踩过的坑](#踩过的坑)
- [已知风险](#已知风险)

---

## 它在做什么

Variational 上黄金有三个可交易标的，**资金费定价机制各不相同**，
于是同一份黄金敞口在不同标的之间存在可捕获的费率差：

| 标的 | 合约类型 | 费率性质 | 交易时段 |
|---|---|---|---|
| `XAU` | `perpetual_rwa_future` | 供需价，**无上限** | 24/7 可交易，但**计息跟随黄金现货时段** |
| `XAUT` | `perpetual_future` | 供需价，**正向封顶 +10.95%/年** | 24/7 |
| `XAUS` | `swap` | OLP 定的类 SOFR **固定融资价** | 跟随黄金现货，**周末休市 49 小时** |

多空双腿等名义反向 → 金价涨跌不赚不亏；收益 = 空头腿收到的费率 − 多头腿付出的费率。

---

## 三个标的与两个结构

**符号约定**（已用账户内 538 条真实结算流水验证，0 条反例）：
`账户现金流 = −(费率 × 有符号持仓)`，即**费率为正时多头付、空头收**。

Swap 的 `long_rate` / `short_rate` 是**持仓方视角**且不对称：
多头付 ~5.72%，空头只收 ~2.50%，中间约 3.22% 是 OLP 的买卖价差。

系统内置三个结构（`tools/hedge_swap_carry.py`）：

| 结构 | 组成 | 适用时段 |
|---|---|---|
| `XAUS_XAU` | 多 XAUS + 空 XAU | **黄金开市**（工作日） |
| `XAU_XAUT` | 多 XAU + 空 XAUT | **黄金长休市**（周末/节假日） |
| `TRIPLE` | 多 XAUS(1) + 多 XAU(1) + 空 XAUT(2) | 备用，当前未启用 |

结构在加载时会校验 **delta 中性**（多腿权重和 == 空腿权重和），不满足直接拒绝。

⚠️ `XAUS_XAU` 要**空 XAU**，`XAU_XAUT` 要**多 XAU`。两者同时持有会被交易所按标的
轧差抵消，产生非预期净持仓。切换逻辑保证任一时刻只持有一个结构。

---

## 为什么要按时段切换

**关键事实：RWA 永续在标的现货市场休市时，资金费被平台清零。**

实测佐证（本仓 `data/swap_carry_samples.jsonl`）：

```
开市  XAU = +9.23% ~ +31.65%   （8 个观测点全为正）
休市  XAU =  0.000%            （12 个观测点精确为零）
```

全平台横切也一致：休市期间 `perpetual_rwa_future` 109 个里仅 5 个非零，
而 `perpetual_future` 436 个全部非零。

于是两个结构天然互补：

| | 黄金开市 | 黄金休市 |
|---|---|---|
| `XAUS_XAU` | XAU费率 − 5.72%，**正** | 0 − 5.72%，**负** |
| `XAU_XAUT` | 10.95% − XAU费率，**常为负** | 10.95% − 0，**正** |

守护进程按黄金现货时段自动切换，两边都吃正的那一侧。

> **注意**：切换本身每年约消耗名义的 6.9%（每周两次、每次四条腿）。
> 「一直持有 `XAUS_XAU` 不切换」的净 carry 其实更高，代价是承担 XAUS 的
> 49 小时冻结窗口。当前配置选择了切换方案，是**用收益换掉那个窗口**。

---

## 五分钟跑起来

```bash
# 1. 环境（Python 3.11）
python3.11 -m venv .venv && .venv/bin/pip install -r requirements.txt

# 2. 凭据：浏览器登录 Variational 后导出会话 Cookie
#    详见 docs/guides/导出-Variational-会话Cookie.md
#    ⚠️ .env 里 VARIATIONAL_COOKIE 必须用单引号包裹（值里含 $，否则被 dotenv 展开）
cp .env.example .env && vi .env

# 3. 自检会话（会打印 Cookie 剩余有效期）
.venv/bin/python -m tools.check_variational_session

# 4. 空跑：走完全部检查与双腿报价，但不成交
.venv/bin/python -m tools.hedge_swap_carry open \
    --structure XAU_XAUT --notional 500 --dry-run

# 5. 确认数量、名义、所需保证金都对，再真下单
.venv/bin/python -m tools.hedge_swap_carry open \
    --structure XAU_XAUT --notional 500 --yes

# 6. 看状态
.venv/bin/python -m tools.hedge_swap_carry status --structure XAU_XAUT

# 7. 平仓
.venv/bin/python -m tools.hedge_swap_carry close --structure XAU_XAUT --yes
```

**面板**（同时看网格、跨所对冲、swap carry）：

```bash
.venv/bin/python -m tools.grid_panel      # 默认 8787 端口，仅监听本机
```

---

## 守护进程做的事

`tools/run_swap_carry_guard.py`，由 launchd 每 5 分钟跑一次 `--once`。

```bash
cp deploy/com.variational.swap-carry-guard.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.variational.swap-carry-guard.plist
```

**按优先级从高到低**，命中即执行并结束本轮：

1. **kill switch** —— `data/swap_carry.kill` 存在 → 平掉全部腿并停止
2. **缺腿 / 方向异常 / 权重比例失衡**（>5%）→ 平仓
3. **强平监控**（按保证金模式分类）
   - isolated 腿（XAUS）：per-leg 强平距离过近**或读不到** → 平仓
   - 全仓腿（XAU/XAUT）：per-leg 强平价缺失**不平仓**，改由账户级保证金率兜底
     （账户权益 ÷ 全部持仓维持保证金，含本结构外的其它策略持仓）
4. **时段元数据异常/陈旧** → 判为不安全 → 平仓
5. **连续 N 轮净 carry ≤ 阈值** → 平仓（计数跨进程持久化）
6. **结构切换** —— 目标结构与当前持仓不符时，先平旧、确认全平、再开新
7. **自动开仓**（优先级最低）—— 六项前置全满足才开：
   空仓 / XAUS 可交易且距休市 >2h / 各腿费率有效 / 净 carry ≥ 门槛 /
   保证金足够 / 当日尝试未超限

**切换后自检**：校验旧结构全平、新结构方向与权重正确、净 delta 在容差内、
无结构外残留腿。任一不满足 → 记 critical + 弹通知 + 置 `switch_incident`，
**后续轮次不再自动开仓与切换**，直到人工清除。

**每轮必写心跳** `data/swap_carry_guard_heartbeat.json`，含结论、各腿、
净 carry、账户保证金率、会话剩余时长。

---

## 日常运维

```bash
# 状态总览
.venv/bin/python -m tools.hedge_swap_carry status

# 切换历史与实测磨损
.venv/bin/python -m tools.show_switch_history --last 5

# 会话剩余有效期
.venv/bin/python -m tools.check_variational_session

# 紧急停止（下一轮即平仓并停机）
touch data/swap_carry.kill

# 只停自动开仓/切换，保留风控
.venv/bin/python -m tools.run_swap_carry_guard --once --no-auto-open
.venv/bin/python -m tools.run_swap_carry_guard --once --no-auto-switch
```

**必须定期做的事：**

- **每 7 天刷新一次 Cookie**。`vr-token` 是 7 天有效期 JWT，过期后守护进程
  失去认证：读不到持仓、平不了仓、切换不会发生。系统会在剩余 <24h 时弹通知、
  <6h 时每轮弹，面板也显示剩余小时数。导出方法见
  `docs/guides/导出-Variational-会话Cookie.md`。
- **看面板的「守护进程心跳」行**。超过 15 分钟会标红——这是发现无声停摆的
  主要手段（本项目有过网格无声停摆 35.5 小时无人发现的事故）。

---

## 踩过的坑

按踩的顺序，每条都真实发生过：

1. **`margin_requirements.initial_margin` 这个字段不存在。**
   真实结构是 `{bid,ask}_margin_delta.initial_margin`，且是**美元金额不是比例**。
   测试之所以全绿，是因为夹具虚构了这个字段。
   → 夹具必须用真实抓包的 schema。

2. **RWA 永续的资金费在标的休市时被清零，不是"费率跌到 0"。**
   在周末读到 `XAU = 0%` 并据此做结构决策，会得出完全相反的结论。
   → 入场判据必须有市场时段感知。

3. **isolated 腿的规则不能套到全仓腿上。**
   曾因「全仓腿 per-leg 强平价读不到 → 按不安全平仓」而误平掉健康仓位。
   API 本来就不保证两侧强平价都返回。
   → 按 `isolated_only` / `margin_mode` 分类，全仓腿看账户级保证金率。

4. **`get_position` 默认子串匹配，`XAU` 会命中 `XAUS`。**
   本结构恰好同账户同时持有这两个。
   → 默认已改为 `exact=True`。

5. **只有入场判据没有出场判据**，会形成闭环陷阱：
   周末读到清零费率 → 开仓 → 周一转负 → 无机制退出 → 扛到下个周末。

6. **`.env` 里 Cookie 不用单引号会被 dotenv 展开 `$`。**

7. **交易日历不规则。** 实测 2026-09-07（劳动节）XAUS 是 18:30Z 收市而非 21:00Z。
   → 必须解析 `trading_sessions`，禁止写死时钟。

---

## 已知风险

**不是方向风险**（delta 中性已消除），而是这四类：

| 风险 | 说明 |
|---|---|
| **资金费转负** | XAUT 历史 24% 的时间为负，最低见过 −18.58%；XAU 可冲到 30%+ |
| **基差** | 两条腿跟的**不是同一个指数**，实测 index 有差、mark 差更大。 周期性平仓会把基差波动变成已实现盈亏 |
| **XAUS 周末冻结** | 49 小时不可交易、不可平仓、不可补保证金；且它是 `isolated_only` |
| **单一对手方** | XAUS 是 RFQ 无订单簿，OLP 是唯一对手方，可随时切 close-only 或改费率 |

**凸性提示**：`XAU_XAUT` 是「做多无顶付款腿 + 做空封顶收款腿」，
最好情况有限、最坏情况无界；`XAUS_XAU` 正好相反。这是选择结构时的重要考量。

**下单受地区限制**：`/quotes/accept` 在受限 IP 上返回 403。守护进程会识别该错误
并弹通知，但**它自己无法处理**——需要人工在放行 IP 上执行。

---

## 相关文档

- `docs/plans/2026-09-05-swap-carry-hedge-plan.md` —— 完整方案与经济模型
- `docs/plans/2026-09-05-swap-carry-首仓执行计划.md` —— 首仓参数与执行步骤
- `docs/guides/导出-Variational-会话Cookie.md` —— Cookie 导出与刷新
- `openspec/changes/variational-swap-carry/` —— 提案、设计决策与需求规范

## 结构切换事前预演

守护进程在计划切换前 30 分钟执行一次 indicative 预演。计划切换时间仍由真实
XAUS 会话边界和 `SWITCH_LEAD_TIME` 推算，不使用固定星期或固定收市时间。
`REHEARSAL_LEAD` 环境变量或 `--rehearsal-lead-minutes` 可覆盖提前分钟数；
错过提前时间的启动会在实际切换前补预演。`--no-auto-switch` 禁用自动预演；
守护进程 `--dry-run` 仍保持原有禁止全部 POST 的约定，不发送 indicative。

预演检查旧仓数量、名义和净 delta，逐腿平仓报价及滑点，新仓数量、名义、保证金，
交易状态与剩余交易时间、各腿年化费率和新结构净 carry。所需保证金按旧结构全平后
的新仓估算：已有同标的仓位时采用报价 `margin_params.params.asset_params` 内的
`futures_initial_margin`，保留原始方向保证金增量供审计，避免把反向抵消旧仓的负增量
误认为新仓不需保证金。可用保证金为 `/portfolio.balance + upnl` 减全部持仓顶层
`initial_margin`，不预支尚未释放的资金。字段不足时阻断。预计耗时仅取历史真实完成
切换的 `total_duration_ms` 中位数；无有效历史明确记录“无估计”。

- `ready`：检查通过，info 记录，不阻止切换。
- `warning`：例如滑点超过 20 bp、净 carry 低于入场阈值，warning 记录，继续允许切换。
- `blocked`：保证金不足、不可交易、报价或检查异常（含 20 秒超时），critical 记录并
  尝试 macOS 通知，本窗口禁止真实切换。同一窗口即使重启或条件恢复也不重复预演。
  直到新窗口重新预演通过才解除；风控平仓后也不能通过自动开仓绕过阻断。

平仓风控优先于预演；blocked 不禁止 reduce-only 风控平仓。注意：按“预演时任何腿
不可交易即 blocked”的规则，开市前预演遇到尚未开市的 XAUS 也会阻断本次开市窗口，
不能仅因随后开市自动解除。

完整证据追加到 `data/swap_carry_switch_history.jsonl`，使用 `kind: "rehearsal"`。
真实切换记录保留关联的预演结论。状态文件保存窗口去重记录；心跳包含 `last_rehearsal`
与 `rehearsal_blocked`，面板显示“上次预演”，阻断显示 critical 告警。查询工具仅读文件：

```bash
.venv/bin/python tools/show_rehearsal.py --last 5
```
