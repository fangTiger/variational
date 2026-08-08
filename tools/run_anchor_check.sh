#!/bin/bash
# 定时状态快照：实盘健康度 + 采集进度 + 趋势样本捕获情况。
# 全只读，不碰账户、不下单。
#
# 注意：原先这里还跑 tools/verify_anchor.py 的证伪判定，已移除——
# 那个判据用 N_osc/2 做闭环上界，推导错误（一次反转在多格行程后会产生
# 一批闭环，不是一个），会持续输出误导性的「模型被证伪」。结论见
# docs/superpowers/specs/2026-08-06-网格收益放大-α测量-design.md 第 10 节。
#
# 现在继续采集的唯一目的：等一段 ADX>30 的趋势行情样本。
cd /Users/captain/python/variational || exit 1
{
  echo "════════════════════════════════════════════════"
  echo "运行时刻 $(date '+%Y-%m-%d %H:%M:%S')"
  echo
  echo "── 实盘状态 ──"
  PYTHONPATH=. .venv/bin/python - <<'PY' 2>&1
import json, datetime
from pathlib import Path

rows = [json.loads(l) for l in Path("data/grid_monitor.jsonl").read_text().splitlines() if l.strip()]
rows = [r for r in rows if r.get("equity")]
if not rows:
    print("  无监控数据")
else:
    last = rows[-1]
    recent = rows[-48:]
    eq = [r["equity"] for r in recent]
    peak = eq[0]
    mdd = 0.0
    for e in eq:
        peak = max(peak, e)
        mdd = max(mdd, peak - e)
    t = datetime.datetime.fromtimestamp(last["ts"])
    print(f"  最新快照 {t:%m-%d %H:%M}  权益 ${last['equity']:.2f}")
    print(f"  库存 ${last['inv_usd']:+.0f}  价格 {last['price']:.0f}  ADX {last.get('adx')}")
    print(f"  frozen={last.get('frozen')} blocked={last.get('blocked_side')} halted={last.get('halted')}")
    print(f"  近 48 小时最大回撤 ${mdd:.2f} ({100 * mdd / peak:.2f}%)")
    if last.get("halted"):
        print("  ⚠⚠ 引擎已 halted，需人工介入")
    if last.get("frozen"):
        print("  ⚠ band 冻结中")
    adx_recent = [r["adx"] for r in rows[-24:] if r.get("adx")]
    if adx_recent and max(adx_recent) >= 30:
        print(f"  ★★ 近 24 小时 ADX 峰值 {max(adx_recent):.1f} ≥ 30 —— 趋势样本正在捕获，"
              f"这是继续采集的目的，可重跑格距分析")
PY
  echo
  echo "── 进程存活 ──"
  launchctl list | grep variational | sed 's/^/  /'
  echo
  echo "── 采集进度 ──"
  PYTHONPATH=. .venv/bin/python - <<'PY' 2>&1
import glob, json
n = g = 0
t0 = t1 = None
for f in glob.glob("data/trades/*.jsonl"):
    for line in open(f):
        if not line.strip():
            continue
        r = json.loads(line)
        if r.get("gap"):
            g += 1
            continue
        n += 1
        if t0 is None or r["T"] < t0:
            t0 = r["T"]
        if t1 is None or r["T"] > t1:
            t1 = r["T"]
if n:
    print(f"  {n} 笔 / {(t1 - t0) / 3600000:.1f} 小时，缺口标记 {g} 条")
else:
    print("  无数据")
PY
  echo
} >> logs/anchor-check.log
