# Variational / Extended 交易机器人

BTC 永续合约的**中性网格策略**实盘系统，跑在 [Extended](https://extended.exchange)（Starknet 上的永续 DEX）。单机 macOS + launchd 守护，全部用限价 maker 单（Extended maker 费为 0）。

仓库里还有一套早期的 Variational 跨所对冲积分 bot（`tools/run_hedge_bot.py`），已停用，保留作参考。

> **风险提示**：这是拿真钱跑的实盘交易系统。策略的盈利能力**尚未被验证**——见下方「当前状态」。任何人在自己账户上运行前，请先用 `dry_run` 模式观察，并从最小金额开始。

---

## 快速开始

### 1. 环境

需要 Python 3.11。

```bash
git clone <repo> && cd variational
python3.11 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

### 2. 配置

在项目根目录创建 `.env`，填入 Extended 的 API 凭据（网格账户用 `X10_GRID_` 前缀）：

```bash
X10_GRID_CLIENT_CONFIG_NAME=MAINNET     # 或 TESTNET
X10_GRID_API_KEY=...                    # Extended 网页端 API 管理页生成
X10_GRID_PUBLIC_KEY=...
X10_GRID_PRIVATE_KEY=...
X10_GRID_VAULT_ID=...
```

`.env` 已在 `.gitignore` 中，不会入库。

### 3. 先跑 dry_run

**不要一上来就用 `--live`。** 默认就是 dry_run，只打印下单意图、不碰账户：

```bash
PYTHONPATH=. .venv/bin/python -m tools.run_grid \
    --trend-aware --spacing 0.001 --unit 50 --levels 10 --max-inv 500
```

观察日志确认行为符合预期后，再加 `--live`。

### 4. 跑测试

```bash
.venv/bin/python -m pytest tests/ -q
```

当前 288 passed。改任何代码前后都应该跑一遍。

---

## 它是怎么工作的

网格策略不预测方向，只做一件事：在当前价上下按固定间距挂满限价单，价格波动时**低买高卖**，每完成一对买卖叫一个**闭环**，赚的就是格距那点价差。

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
| `adapters/extended_client.py` | Extended 交易所适配器（下单/撤单/查仓/TPSL） |
| `tools/run_grid.py` | 守护进程入口 |
| `tools/grid_monitor.py` | 每小时权益快照 → `data/grid_monitor.jsonl` |
| `tools/alert_check.py` | 异常主动告警（macOS 通知） |
| `tools/grid_panel.py` | 本地网页面板 `http://localhost:8787` |

---

## 风控：三层，各管一段

| 层 | 参数 | 作用 |
|---|---|---|
| 整仓 TPSL | `max_equity_loss_pct=0.10` | 交易所侧的止损单，单次止损浮亏 ≈ **权益 10%**，与持仓大小无关 |
| 硬止损 | `--hard-stop-dist 0.12` | 距强平价 12% 时全平 |
| 净值回撤熔断 | `--max-drawdown 0.12` | 自历史峰值回撤 12% → 全平停机，**需人工复位** |

**为什么要第三层**：前两层都是「每腿」保护——止损棘轮在仓位翻向时重置，而网格库存频繁穿零，连续阴跌里每腿各亏 10% 会复利，三腿就是 −27%。熔断是唯一盯着累计净值的约束。

熔断触发后不会自动恢复（自动恢复会在同一段行情里反复触发、越平越亏）。复位方法：编辑 `data/grid_state.json` 把 `halted` 改回 `false`，然后重启引擎。

**出入金注意**：熔断的峰值基准存在 `data/equity_peak.json`，它区分不了「出金」和「亏损」。**出入金后请手动删除这个文件**，让它按新本金重新播种，否则一笔大额出金会被当成回撤而触发平仓。

---

## 日常运维

### launchd 服务

所有服务的 plist 在 `~/Library/LaunchAgents/`：

| 服务 | 频率 | 作用 |
|---|---|---|
| `com.variational.grid-bot` | 常驻（KeepAlive） | 网格引擎主进程 |
| `com.variational.grid-monitor` | 每小时 | 权益快照 |
| `com.variational.alert-check` | 每 15 分钟 | 异常告警 |
| `com.variational.anchor-check` | 每天 9:00 / 21:00 | 健康巡检 |
| `com.variational.trade-collector` | 常驻 | 逐笔成交采集 |

```bash
launchctl list | grep variational          # 看状态（第二列是上次退出码）
tail -f logs/grid-bot.err.log              # 看引擎日志
launchctl kickstart -k gui/$(id -u)/com.variational.grid-bot   # 重启（仅重载代码）
```

### 改参数（有坑，务必按顺序）

参数写在 `~/Library/LaunchAgents/com.variational.grid-bot.plist` 的 `ProgramArguments` 里。

```bash
# 1. 先撤掉所有旧网格单——否则旧单占着库存额度，新单一个也挂不上，网格会空转
PYTHONPATH=. .venv/bin/python - <<'PY'
import asyncio
from adapters.extended_client import ExtendedClient, filter_grid_orders
async def main():
    ext = ExtendedClient.from_env(prefix="X10_GRID"); await ext.connect()
    orders = await ext.get_open_orders("BTC-USD")
    ids = [int(o.id) for o in filter_grid_orders(orders)]   # 只撤普通网格单，保留 TPSL
    await ext._client.orders.mass_cancel(order_ids=ids)
    await ext.close()
asyncio.run(main())
PY

# 2. 改 plist
/usr/libexec/PlistBuddy -c "Set :ProgramArguments:12 2500" ~/Library/LaunchAgents/com.variational.grid-bot.plist
plutil -lint ~/Library/LaunchAgents/com.variational.grid-bot.plist

# 3. 必须 bootout + bootstrap——kickstart 不会重读 plist！
launchctl bootout gui/$(id -u)/com.variational.grid-bot
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.variational.grid-bot.plist

# 4. 确认日志里打印的是新参数值
grep "网格引擎启动" logs/grid-bot.err.log | tail -1
```

### 查看状态

```bash
cat data/grid_live.json | python3 -m json.tool     # 引擎实时快照
PYTHONPATH=. .venv/bin/python -m tools.grid_monitor --report    # 权益/回撤汇总
PYTHONPATH=. .venv/bin/python -m tools.alert_check --dry-run    # 手动跑一次告警检查
nohup .venv/bin/python -m tools.grid_panel --port 8787 &        # 网页面板
```

**判断真实冻结状态要看 `logs/grid-bot.err.log` 里的 `trend-aware` 行**，不能只信 `grid_live.json` 的 `blocked_side`——后者只记 band 突破，不含 OFF 模式合成的双向封锁。

---

## 历史教训（血泪换来的，别重蹈覆辙）

**1. 不要启用任何形式的全局方向冻结**（ADX 熔断 / Donchian 突破急停）

已经三次被实盘证伪。最严重的一次（2026-08-10）：ADX 熔断触发后，冻结只挡住**新挂单**，挡不住盘口上已有的买单继续成交，而成交后的卖出翻单被冻结阻止——网格退化成**单向抄底**，库存被打成 $1687 的裸多头卡死 10.5 小时。

根本原因：全局方向冻结与「成交后必须翻单闭环」的网格语义天然冲突。趋势风险应由**库存上限 + 格距**承担。所以 `--adx-off` / `--adx-resume` 默认都是 999（禁用）。

**2. 格距必须与波动率和轮询频率配套**

格距 1.5% 时曾隔夜 9 小时零成交——那是 BTC 小时 ATR 的 3 倍，价格得单向走 3 小时才够一格。当前格距 ≈ 0.35×ATR，轮询 2.5 秒。

**3. 判据必须能被交叉验证**

早期一套「α 测量」框架用 `N_osc/2` 作为闭环率上界，推导是错的（一次反转在多格行程后产生一批闭环而非一个），据此得出的结论全部作废。`tools/verify_anchor.py` 保留作为反面教材，文件顶部有失效警告。

**4. 长跑进程要定期重启**

aiohttp 连接池会老化，曾导致持续 "Connection reset by peer"。重启即恢复。

---

## 当前状态与已知限制

- **策略盈利能力尚未验证**。账面收益里，网格闭环利润与持仓方向性盈亏混在一起，目前无法区分。收益归因数据层正在实施中（设计见 `docs/superpowers/specs/2026-08-11-*.md`，计划见 `docs/superpowers/plans/2026-08-13-*.md`）
- 归因上线后进入 **4 周验证期**，判据事先定死：闭环年化 < 15%、或净值年化 ≤ 0、或最大回撤 ≥ 12% —— 任一条满足即停
- 这是**卖波动率 / 做空 gamma** 结构：赚小额高频、赔在尾部。4 周只能证伪不能证实
- **单点故障**：全部跑在一台 Mac 上，断电或断网即全停（交易所侧只剩 TPSL 兜底）
- 网络质量对结果影响很大：单请求超过 5 秒是常态（约 3%），HTTP 超时已放宽到 25 秒

---

## 目录结构

```
grid/          策略与引擎（regime/band/risk 均为可测纯函数）
adapters/      交易所适配器
tools/         守护进程、监控、告警、面板、分析脚本
tests/         pytest 测试（288 passed）
infra/         日志、SSL 等基础设施
data/          运行数据（不入库）
logs/          日志（不入库）
docs/          设计文档与实施计划
```

## 开发约定

- 文档和代码注释用中文，标识符用英文
- 改动前先跑测试，改动后再跑一遍
- 风控相关代码的测试**必须从失败路径写起**——测试桩总是返回成功，会把严重缺陷完整放过（真实教训）
- 提交信息说明「为什么」，不只是「改了什么」
