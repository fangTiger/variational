# 多交易所永续对冲机器人

在 A 交易所开仓、B 交易所同时开等量反向仓，**净方向敞口恒为零**。
目标是刷出成交量赚积分，不赚价差。支持同时跑多对互不干扰的对冲。

> **拿真钱跑的实盘系统。** 对冲消除的是方向风险，**不是交易成本**——手续费、
> 滑点、跨所基差是确定的持续支出。默认 dry-run，务必先观察再上小额。

---

## 五分钟跑起来

```bash
# 1. 装环境（需要 Python 3.11）
git clone git@github.com:fangTiger/variational.git && cd variational
python3.11 -m venv .venv && .venv/bin/pip install -r requirements.txt

# 2. 填凭据（只填你要用的那两个交易所，格式见「配置」）
cp .env.example .env && vi .env

# 3. 空跑一次，只打印配置摘要，不连交易所
PYTHONPATH=. .venv/bin/python -m tools.run_timed_volume \
    --primary-venue lighter --market BTC \
    --hedge-venue hyperliquid --hedge-market BTC \
    --notional-min 100 --notional-max 120 --cycle-hours 4

# 4. 确认摘要里的交易所、账户、金额都对，再加 --live 真实下单
#    先用能承受的最小金额跑一轮，别直接上大额

# 5. 开面板看持仓
nohup .venv/bin/python -m tools.hedge_panel --port 8787 &
open http://localhost:8787
```

**只有加了 `--live` 才会真实下单**，不加就只打印配置。

**健康与否只看两个数**：净敞口接近 0、互锁未激活。面板上都有。

---

## 支持哪些对冲组合

四个交易所适配器，主腿与对冲腿可自由组合：

| 主腿 ↓ / 对冲腿 → | Extended | Hyperliquid（Entropy） | Variational |
|---|---|---|---|
| **Lighter** | ✅ | ✅ **实盘运行中** | ✅ |
| **Variational** | ✅ | ✅ **实盘运行中** | —（同所自对冲，已被拦截） |

```bash
--primary-venue {lighter, variational}
--hedge-venue   {extended, hyperliquid, variational}
```

> **Entropy 不是独立交易所**，它是 Hyperliquid 的第三方前端，共用同一个适配器，
> 靠 Builder Code 把成交量归属过去。

**适配器能力矩阵**（决定了哪些组合真正可用）：

| | Extended | Lighter | Hyperliquid | Variational |
|---|---|---|---|---|
| 市价单 | ✅ | ✅ | ✅ | ✅ |
| 限价单 / maker | ✅ | ✅ | ✅ | ❌ **RFQ 模型，没有限价单** |
| 手续费 | 有 | 有 | 有 | **零**（成本在报价价差里） |
| 持仓盈亏 | ❌ | ✅ | ✅ | ✅ |
| 账户权益 | ✅ | ✅ | ✅ | ❌ |
| 整仓止损 | ✅ | ✅ | ❌ | ❌ |

三个要注意的缺口：

- **Variational 没有限价单**，这是 RFQ 询价模型决定的，不是没实现。它永远吃单，
  靠零手续费补偿。执行层按适配器声明的 `execution_model` 自动分流，
  RFQ 腿直接走市价，订单簿腿保持 maker-first。
- **Extended 没有 `get_position_pnl`**，用它当对冲腿时面板上那条腿的盈亏显示 `—`。
- **Hyperliquid 与 Variational 都没有整仓止损**。定时定量不依赖它（每轮定时平仓），
  但**要在这两个所上跑网格必须先补**——网格三层风控依赖交易所侧止损。

### 选币种：价差实测

Variational 是 RFQ 模型，价差几乎与币种无关；Lighter 是订单簿，深度差异极大。
两者相加约等于一轮开平的价差成本：

| 币种 | Variational | Lighter | 合计 |
|---|---|---|---|
| BTC | 0.0114% | **0.0001%** | **0.0115%** |
| ETH | 0.0115% | 0.0135% | 0.0250% |
| SOL | 0.0103% | 0.0319% | 0.0422% |

**结论与直觉相反**：Variational 对 ETH 并不比 BTC 差（$5000 询价时价差也只从
0.0115% 升到 0.0123%），真正拉开差距的是 Lighter 那一侧。

⚠️ 但**同一账户的同一市场不能被两个实例同时使用**——两者会各自 `get_position()`
读到对方的仓位，双方都误判对冲状态。所以第二对若共用账户，必须换币种。
启动时有强制校验，见「风控」。

---

## 配置

在项目根目录创建 `.env`，**只需填你要用的那两个交易所**：

```bash
# ---- Lighter ----
LIGHTER_RH_L1_ADDRESS=0x...             # Robinhood Wallet 地址
LIGHTER_API_PRIVATE_KEY=...             # Lighter API 私钥
LIGHTER_API_KEY_INDEX=255

# ---- Hyperliquid / Entropy ----
HYPERLIQUID_ACCOUNT_ADDRESS=0x...       # 主钱包地址（不是私钥）
HYPERLIQUID_AGENT_PRIVATE_KEY=0x...     # API 代理钱包私钥，只能交易不能提现
HYPERLIQUID_BUILDER_ADDRESS=0x...       # 第三方前端的 builder code 地址
HYPERLIQUID_BUILDER_FEE_TENTHS_BPS=1    # ⚠️ 不能为 0，见下方说明

# ---- Hyperliquid 第二账户（跑第二对时用，前缀任意）----
HYPERLIQUID_VAR_ACCOUNT_ADDRESS=0x...
HYPERLIQUID_VAR_AGENT_PRIVATE_KEY=0x...
HYPERLIQUID_VAR_BUILDER_ADDRESS=0x...
HYPERLIQUID_VAR_BUILDER_FEE_TENTHS_BPS=1

# ---- Variational（浏览器会话 Cookie 授权）----
VARIATIONAL_COOKIE='k=v; k2=v2; ...'    # ⚠️ 用单引号，见下
VARIATIONAL_WALLET_ADDRESS=0x...
VARIATIONAL_USER_AGENT=Mozilla/5.0 ...  # 必须与导出 Cookie 的浏览器一致

# ---- Extended（网格用，当前已停用）----
X10_GRID_CLIENT_CONFIG_NAME=MAINNET
X10_GRID_API_KEY=...
X10_GRID_PUBLIC_KEY=...
X10_GRID_PRIVATE_KEY=...
X10_GRID_VAULT_ID=...
```

**多账户靠环境变量前缀隔离**：`--hedge-env-prefix HYPERLIQUID_VAR` 即从该前缀读取
账户地址、代理私钥与 builder 配置。同时跑多对时，各对必须指向不同账户或不同市场。

关于 Variational 的三个坑（都实际踩过）：

- **Cookie 有效期约 7 天**，`cf_clearance` 还绑定「IP + 浏览器 UA」。
  Chrome 升级大版本后 UA 对不上会被 Cloudflare 拦，**症状和 Cookie 过期一模一样**，
  重导多少次都没用——先核对 `.env` 里的 UA。导出流程见
  `docs/guides/导出-Variational-会话Cookie.md`。
- **`.env` 里 Cookie 要用单引号**，否则 dotenv 会把值里的 `$` 当变量展开。
- 抓 Cookie 时**DevTools 必须先打开再硬刷新**，否则只有 "Provisional headers"；
  且要挑需要登录的接口（`/api/positions`），公开接口不带 Cookie。

关于 Hyperliquid 的三个要点：

- **用 API 代理钱包（agent wallet）**，主钱包私钥不必落地。代理钱包只能交易、
  不能提现，因此放进 `.env` 是安全的。它的地址与登录地址不同，属正常现象。
- **是统一账户**：Spot 余额可直接作为永续保证金，**不需要 Spot→Perps 划转**。
  另外 agent wallet 只能交易，**做不了资金划转**（`usd_class_transfer` 会被拒）。
- **`HYPERLIQUID_BUILDER_FEE_TENTHS_BPS` 绝不能填 0**。协议层面 `f=0` 等价于
  「无 builder」（撤销 builder 授权的方式正是把费率设为 0），交易将**完全不归属**。
  实测对照：`f=0` 的成交记录无 `builderFee` 字段，`f=1` 才有。

`.env` 已在 `.gitignore` 中，不会入库。改任何代码前后都跑一遍测试：
`.venv/bin/python -m pytest tests/ -q`

---

## 用法与参数

### 这个仓库有六套系统，只有第一套是在跑的

| 系统 | 入口 | 交易所 | 状态 |
|---|---|---|---|
| **定时定量对冲刷量** | `tools/run_timed_volume.py` | 任意两所组合 | ✅ **实盘运行中，当前主体** |
| **对冲持仓面板** | `tools/hedge_panel.py` | —（只读本地文件） | ✅ 运行中，`http://localhost:8787` |
| BTC 中性网格 | `tools/run_grid.py` | Extended | ⛔ 已停用（2026-08-19 事故后），代码与风控保留 |
| Lighter 做市（网格式） | `tools/run_lighter_mm.py` | Lighter | ⛔ 已停用，被定时定量策略取代 |
| Lighter RH 积分对冲 | `tools/run_lighter_hedge.py` | Lighter + Extended | ⛔ 已停用，代码保留 |
| 早期跨所对冲刷积分 | `tools/run_hedge_bot.py` | Variational + Extended | ⛔ 已停用，保留作参考 |

定时定量当前跑着**两对实例**：Lighter × Entropy 与 Variational × Entropy，
各用独立状态文件、独立心跳、独立 Hyperliquid 账户。

**为什么主体从网格换成了定时定量**（两条实测结论）：

1. **网格频率不可控**——成交由价格波动决定、不由时间决定。实测 54.8~102 笔/小时，
   要降到「2 小时一次」需把格距放到 10% 以上，届时可能几天不成交。
2. **网格的对冲滞后损失是主要成本**——库存持续变化，对冲腿永远在追一个已经变了的
   目标。实测**成本率 0.144%**（成本 ÷ 成交额），是 Extended taker 费的 6.4 倍。
   定时定量则是开仓与对冲**同步下单**，实测成本率低一个数量级。

**账户前缀约定**：涉及 Extended 的系统按用途使用前缀——网格用 `X10_GRID_`，
旧对冲用 `X10_`，Lighter RH 对冲用 `X10_HEDGE_`。⚠️ 实测 `X10_` 与 `X10_GRID_`
指向**同一个 vault**，并非三个独立账户——上线前务必核对 vault id。

### 定时定量对冲刷量（当前主体）

默认 dry-run，只打印配置摘要；**必须显式 `--live` 才会连接交易所下单**。

```bash
# 第一对：Lighter × Entropy
PYTHONPATH=. .venv/bin/python -m tools.run_timed_volume --live \
    --primary-venue lighter --market BTC \
    --hedge-venue hyperliquid --hedge-market BTC \
    --notional-min <下限> --notional-max <上限> \
    --cycle-hours 4 --initial-direction long \
    --maker-timeout 300 --poll-interval 30

# 第二对：Variational × Entropy（注意三处隔离）
PYTHONPATH=. .venv/bin/python -m tools.run_timed_volume --live \
    --primary-venue variational --market BTC \
    --hedge-venue hyperliquid --hedge-env-prefix HYPERLIQUID_VAR \
    --hedge-market BTC \
    --notional-min <下限> --notional-max <上限> \
    --cycle-hours 4 --initial-direction short \
    --state-path data/timed_volume_var/state.json \
    --heartbeat-path data/timed_volume_var.jsonl
```

| 参数 | 说明 |
|---|---|
| `--primary-venue` | 主腿：`lighter` / `variational` |
| `--hedge-venue` | 对冲腿：`hyperliquid` / `extended` / `variational` |
| `--hedge-env-prefix` | 对冲腿凭据的环境变量前缀，**多实例隔离账户靠它** |
| `--notional-min/max` | 每轮单边名义额区间，**每轮随机取整数**以打散规律性。<br>取值须让两侧权益都能覆盖，别照抄示例 |
| `--cycle-hours` | 持仓周期。到期平仓后立即反向开下一轮 |
| `--initial-direction` | 无历史记录时的首轮方向，之后逐轮交替 |
| `--maker-timeout` | maker 优先等待秒数，超时才转市价。**订单簿腿的主要成本杠杆** |
| `--state-path` / `--heartbeat-path` | **多实例必须各自指定**，否则轮次状态互相覆写 |

**同时跑多对的三条隔离要求**（前两条有启动校验强制，第三条靠自觉）：

1. **状态文件不同** —— 有进程级排他锁，重复占用会被拒绝启动
2. **(交易所, 账户, 市场) 三元组不重复** —— 有跨实例占用检测
3. 两侧权益要能覆盖**各自账户上所有实例的名义额之和**

**关于 `--maker-timeout`**：盘口实测 15 秒的 maker 成交率仅 **10.8%**，
300 秒可达 **68.5%**。实盘按成交额口径统计约 **51%~61%** 走 maker。
定时策略不赶时间，设长有明显收益。对 RFQ 腿（Variational）此参数无效——
它没有限价单，永远吃单。

**后台常驻**：macOS **没有 `setsid`**，直接 `nohup &` 起的进程会被调用方的
超时信号连带杀掉（已实际发生过）。需要用 Python 的 `start_new_session=True`
另起会话。

```bash
tail -f logs/timed_volume.log                            # 主日志
tail -1 data/timed_volume.jsonl | python3 -m json.tool   # 心跳快照
cat data/timed_volume/state.json                         # 当前轮次状态
```

**判断运行是否健康，只看两个字段**：`net_exposure` 接近 0、
`hedge_interlock_active` 为 `false`。或者直接开面板。

### 方式三：辅助工具（按需手动跑）

```bash
# 对冲持仓面板 http://localhost:8787
nohup .venv/bin/python -m tools.hedge_panel --port 8787 > logs/hedge-panel.out.log 2>&1 &

# 网格面板（网格已停用，保留）
# nohup .venv/bin/python -m tools.grid_panel --port 8788 > logs/grid-panel.out.log 2>&1 &

# 权益/回撤汇总
PYTHONPATH=. .venv/bin/python -m tools.grid_monitor --report

# 手动运行一次收益归因；报告写入 data/attribution.json
PYTHONPATH=. .venv/bin/python -m tools.pnl_attribution --report
python3 -m json.tool data/attribution.json

# 手动跑一次告警检查（--dry-run 只打印不弹通知）
PYTHONPATH=. .venv/bin/python -m tools.alert_check --dry-run

# 断网前紧急收摊：撤单 + 平仓
PYTHONPATH=. .venv/bin/python -m tools.go_dark
```

---

## 它是怎么工作的

### 定时定量对冲（当前主体）

不预测方向，也不赚价差。每轮固定动作：

```
T=0h    主腿   开仓 $N          ┐ 同一节拍并发提交
        对冲腿 反向 $N          ┘ → 净敞口 ≈ 0
T=4h    两侧同步平仓             → 归零，下一轮方向取反
```

每轮的名义额在区间内**随机取整**，先落盘再下单——先记录后执行，
崩在中间时两边金额才不会对不上。

**收益来自成交量对应的积分，不来自价差**——闭环利润被对冲抵消是预期结果，
不是 bug。唯一的支出是手续费、滑点与跨所基差。

执行层按适配器的 `execution_model` 分流：订单簿腿走 maker-first
（挂限价 → 超时转市价），RFQ 腿直接市价。分流点在取行情**之前**，
因为 Variational 的 `get_market_price` 内部是一次真实询价，有实际成本。

安全上只有一条铁律：**任何时刻不得留下单边裸仓**。所以

- 一侧成交、另一侧超时未成 → **回滚已成交的那一侧**
- 任一侧部分成交 → 按**两侧实际持仓差**补齐，绝不按委托量推断
- 对冲腿不可用或主腿认证失效 → 互锁触发，**停止开新仓**但仍允许平仓

### 网格（已停用，代码保留）

网格在当前价上下按固定间距挂满限价单，价格波动时**低买高卖**，每完成一对买卖叫一个**闭环**，赚的就是格距那点价差。

```
价格 ↑
  │  ── SELL 64200   ← 挂着等成交
  │  ── SELL 64130
  │  ── SELL 64060
  │      现价 64000
  │  ── BUY  63940
  │  ── BUY  63870   ← 成交后立刻在上一格挂 SELL 止盈
  │  ── BUY  63800
```

买单成交后立即在上一格挂卖单，卖单成交后在下一格挂买单——这个「翻单」动作是网格的核心，任何阻断它的机制都会让策略退化（见下方「历史教训」）。

### 核心模块

| 路径 | 职责 |
|---|---|
| `grid/grid_engine.py` | 实盘引擎：挂撤单、成交对账、库存管理、风控。系统的心脏 |
| `grid/regime.py` | 市况判定（ADX / ATR / Donchian），纯函数 |
| `grid/band.py` | 有界通道：价格越出固定区间时冻结加仓侧 |
| `grid/risk.py` | 距强平距离与硬止损判定，纯函数 |
| `timed_volume/strategy.py` | **定时定量对冲策略**：轮次调度、方向交替、随机名义额、双边收敛、认证自愈 |
| `engine/hedge_engine.py` | 执行层：订单簿腿走 maker-first（超时降级吃单），RFQ 腿直接市价 |
| `adapters/base.py` | 适配器契约：`execution_model`、`PositionPnl` 等可选能力 |
| `adapters/extended_client.py` | Extended 适配器（下单/撤单/查仓/TPSL） |
| `adapters/lighter_client.py` | Lighter 适配器 |
| `adapters/hyperliquid_client.py` | Hyperliquid 适配器，含 Builder Code 归属（Entropy） |
| `adapters/variational_client.py` | Variational 适配器，**RFQ 询价模型**，Cookie 会话授权 |
| `tools/run_timed_volume.py` | **定时定量对冲入口**（当前主体），含实例锁与占用检测 |
| `tools/hedge_panel.py` | **对冲持仓面板** `http://localhost:8787`，只读本地文件、零凭据 |
| `tools/check_variational_session.py` | Variational 会话只读自检，不下单 |
| `tools/run_grid.py` | 网格守护进程入口 |
| `tools/grid_monitor.py` | 每小时权益快照 → `data/grid_monitor.jsonl` |
| `tools/pnl_attribution.py` | 导入成交、配对闭环、校验残差 → `data/attribution.json` |
| `tools/alert_check.py` | 异常主动告警（macOS 通知） |
| `tools/grid_panel.py` | 网格面板（网格已停用） |

---

## 风控

### 定时定量策略的保护

| 层 | 作用 |
|---|---|
| **单边收敛** | 一侧成交另一侧没成交 → 回滚，**绝不留裸仓**。所有补单按实际持仓差计算 |
| **对冲存活互锁** | 对冲腿不可用 → 停止开新仓，但仍允许平仓与撤单 |
| **认证失效自愈** | Variational 的 Cookie 过期时重建客户端，**重建后要用真实 `get_position()` 验证才接受**；仍失败则置互锁停止开新仓，等人工更新 |
| **对冲容差** | 净敞口小于两侧最小下单量中较大者即视为已对冲，避免跨所精度差造成死循环 |
| **轮次状态持久化** | 重启后沿用原轮次与金额；与实际持仓不符时**以实际持仓为准** |
| **状态文件排他锁** | `fcntl.flock` + PID + owner token。进程崩溃时内核自动释放，不会留下永久锁死的状态文件 |
| **跨实例占用检测** | 启动时扫描存活实例的锁，`(交易所, 账户指纹, 市场)` 三元组冲突则拒绝启动。<br>账户指纹取 sha256 前 12 位，锁文件里不写原始地址 |
| **同实例双腿校验** | 同平台同账户同市场的自我对冲配置直接拒绝启动 |

⚠️ **盈亏读取与交易完全隔离**：面板用的盈亏数据是装饰性的，`run_once` 先跑完
整个交易状态机再附加盈亏，整段包在 try/except 里。已实测：两腿盈亏接口同时抛异常，
策略仍正常开仓、两腿都下单、互锁未误触发。装饰性功能不得拖垮核心链路。

⚠️ **互锁必须是方向性门控，不能是全局冻结**。2026-08-10 的教训：全局冻结挡住了
新挂单却挡不住盘口上已有的单继续成交，而成交后的翻单被冻结阻止，
库存被打成满额裸多头卡死 10.5 小时。

### 网格的三层风控（已停用系统，逻辑保留）

| 层 | 参数 | 作用 |
|---|---|---|
| 整仓 TPSL | `max_equity_loss_pct=0.10` | 交易所侧的止损单，单次止损浮亏 ≈ **权益 10%**，与持仓大小无关 |
| 硬止损 | `--hard-stop-dist 0.12` | 距强平价 12% 时全平 |
| 净值回撤熔断 | `--max-drawdown 0.12` | 自历史峰值回撤 12% → 全平停机，**需人工复位** |

外加 2026-08-19 事故后新增的三项：**启动时风控完整性自检**（依赖能力缺失即
拒绝启动）、**权益比例库存上限**（`min(权益×比例, 绝对硬顶)`）、
**交易时段窗口**。

**为什么要第三层**：前两层都是「每腿」保护——止损棘轮在仓位翻向时重置，而网格库存频繁穿零，连续阴跌里每腿各亏 10% 会复利，三腿就是 −27%。熔断是唯一盯着累计净值的约束。

熔断触发后不会自动恢复（自动恢复会在同一段行情里反复触发、越平越亏）。复位方法：编辑 `data/grid_state.json` 把 `halted` 改回 `false`，然后重启引擎。

**出入金注意**：熔断的峰值基准存在 `data/equity_peak.json`，它区分不了「出金」和「亏损」。**出入金后请手动删除这个文件**，让它按新本金重新播种，否则一笔大额出金会被当成回撤而触发平仓。

---

## 日常运维

### 看日志

```bash
tail -f logs/grid-bot.err.log                    # 引擎主日志（成交/挂单/异常）
tail -f logs/lighter-hedge.err.log               # Lighter 对冲标准错误
tail -20 logs/alert-check.log                    # 告警检查记录
tail -40 logs/anchor-check.log                   # 每日巡检报告
```

引擎日志已做降噪：正常轮次每 200 轮汇总一行，只有耗时 ≥5 秒的**慢轮**才逐条记录（那是排查网络超时的关键线索）。同类连接错误每 50 次折叠成一条，避免故障期日志爆炸。

### 查看状态

```bash
cat data/grid_live.json | python3 -m json.tool     # 引擎实时快照
tail -3 data/lighter_hedge.jsonl                   # Lighter 对冲最近三轮心跳
python3 -m json.tool data/attribution.json         # 闭环收益、归因残差与 4 周判据
PYTHONPATH=. .venv/bin/python -m tools.grid_monitor --report    # 权益/回撤汇总
PYTHONPATH=. .venv/bin/python -m tools.alert_check --dry-run    # 手动跑一次告警检查
nohup .venv/bin/python -m tools.grid_panel --port 8787 &        # 网页面板
```

**判断真实冻结状态要看 `logs/grid-bot.err.log` 里的 `trend-aware` 行**，不能只信 `grid_live.json` 的 `blocked_side`——后者只记 band 突破，不含 OFF 模式合成的双向封锁。

---

## 历史教训（血泪换来的，别重蹈覆辙）

**1. 不要启用任何形式的全局方向冻结**（ADX 熔断 / Donchian 突破急停）

已经三次被实盘证伪。最严重的一次（2026-08-10）：ADX 熔断触发后，冻结只挡住**新挂单**，挡不住盘口上已有的买单继续成交，而成交后的卖出翻单被冻结阻止——网格退化成**单向抄底**，库存被打成满额裸多头卡死 10.5 小时。

根本原因：全局方向冻结与「成交后必须翻单闭环」的网格语义天然冲突。趋势风险应由**库存上限 + 格距**承担。所以 `--adx-off` / `--adx-resume` 默认都是 999（禁用）。

**2. 格距必须与波动率和轮询频率配套**

格距 1.5% 时曾隔夜 9 小时零成交——那是 BTC 小时 ATR 的 3 倍，价格得单向走 3 小时才够一格。当前格距 ≈ 0.35×ATR，轮询 2.5 秒。

**3. 判据必须能被交叉验证**

早期一套「α 测量」框架用 `N_osc/2` 作为闭环率上界，推导是错的（一次反转在多格行程后产生一批闭环而非一个），据此得出的结论全部作废。`tools/verify_anchor.py` 保留作为反面教材，文件顶部有失效警告。

**4. 长跑进程要定期重启**

aiohttp 连接池会老化，曾导致持续 "Connection reset by peer"。重启即恢复。

---

## 当前状态与已知限制

### 定时定量对冲（运行中）

两对实例的实测表现（比例口径；绝对金额见本地私有记录）：

| 指标 | Lighter × Entropy | Variational × Entropy |
|---|---|---|
| 对冲精度 | 净敞口 / 名义额 **< 0.06%**，全程无单边裸仓 | 同左 |
| 成本率 | **0.02~0.03%** | **约 0.06%**（见下方待解问题） |
| maker 成交额占比 | 51.6% | 61.3% |
| 持仓读取失败率 | **5.6%** | **0.17%** |
| 方向交替 | 全程无一轮失序 | 全程无一轮失序 |

**这个亏损是刷量的门票钱，不是策略失败**——方向盈亏已被对冲抹平，
剩下的是跨所交易的固有摩擦，压不到零。

已知问题：

- **Variational 那对的成本率比预期高 2~3 倍，尚未完全归因。** 显性成本
  （手续费 + 资金费）只占约四分之一，其余是滑点与基差。已查实两条线索：
  Variational 的报价价差是 Hyperliquid 的 **8.9 倍**（0.0114% vs 0.0013%，
  但它零手续费）；两所存在持续基差，Variational 中价系统性低约 **0.10%**。
  理论上**恒定**基差在开平仓时自行抵消，真正产生成本的是持仓期间基差的**变化**——
  目前只有单时点采样，**不足以断言这是主因**。样本仅 2.5 轮，需继续观察。
- **`position_read_failed` 在 Lighter 侧约占轮询 5.6%**（Variational 侧仅 0.17%，
  稳定得多）。根因是**瞬时 `ConnectError` / 超时**（API 本身正常，数十秒自愈）。
  策略处理正确——读不到持仓就既不开仓也不平仓，保守跳过。但**日志里异常详情为空**
  （`TimeoutError` 的 `str()` 就是空字符串），排查困难，且尚未加重试。
- **Entropy 归属只验证到链上这一环**。两个账户的成交记录都已确认带 `builderFee`，
  但其界面是否统计仍待确认——若它依赖自家后端（`api.entropy.trade`）记账，
  绕过前端直连 Hyperliquid 可能仍不计入。
- **日志每行被记录两次**（logging 配置重复挂了 handler），日志体积白白翻倍。
- **对冲腿日志文案硬编码为「Extended」**，实际可能是任意交易所，容易误导。
- **`get_liquidation_info()` 的维持保证金写死 0.1**，而 Variational 的 BTC 实为
  **0.0125**（`use_default_asset_param: false`），算出的清仓价比实际近 8 倍。
  方向上是过度保守而非危险，且定时定量不依赖该函数，但**接网格前必须修**。

### 网格（已停用）

2026-08-19 BTC 单日涨 8.5%，Lighter 侧因三层风控 flag **一个都没传**而满仓扛空
（库存打满并越过上限，达 105%），Extended 侧靠 `--trend-aware` 主动止损离场。
事故后补齐了风控自检、权益比例上限、交易时段窗口，但主体已换成定时定量策略。

网格是**卖波动率 / 做空 gamma** 结构：赚小额高频、赔在尾部。三次实测均为
「闭环持续赚钱、方向腿赚得更多地亏回去」。

### 通用

- **单点故障**：全部跑在一台 Mac 上，断电或断网即全停
- 网络质量对结果影响很大：单请求超过 5 秒是常态，两个交易所都出现过瞬时连接故障

---

## 目录结构

```
timed_volume/  定时定量对冲策略（当前主体）
engine/        执行层：maker-first 与 RFQ 分流
grid/          网格策略与引擎（regime/band/risk 均为可测纯函数，已停用）
adapters/      交易所适配器（Extended / Lighter / Hyperliquid / Variational）
tools/         守护进程、监控、告警、面板、分析脚本
tests/         pytest 测试（1006 passed）
infra/         日志、SSL 等基础设施
openspec/      变更提案与规范
data/          运行数据（不入库）
logs/          日志（不入库）
docs/          设计文档与实施计划
docs/private/  实盘金额与账户标识（不入库）
```

⚠️ **本仓库是公开的**。金额、权益、持仓规模、账户标识一律不写进 README 或代码，
只放 `docs/private/`（已在 `.gitignore`）。文档里用比例口径代替绝对金额。

## 开发约定

- 文档和代码注释用中文，标识符用英文
- 改动前先跑测试，改动后再跑一遍
- 风控相关代码的测试**必须从失败路径写起**——测试桩总是返回成功，会把严重缺陷完整放过（真实教训）
- 提交信息说明「为什么」，不只是「改了什么」
