"""格距标度指数分析（离线只读）。

读取 trade_collector 落盘的逐笔成交，计算振荡数与局部标度斜率 α(s)，
按 ADX regime 分档输出，并给出「当前格距该往宽还是往窄挪」的建议。

只读：除 stdout 外不写任何文件，不碰账户，不 import grid_engine。

用法：
    PYTHONPATH=. .venv/bin/python -m tools.analyze_alpha
    PYTHONPATH=. .venv/bin/python -m tools.analyze_alpha --days 3 --by-regime
"""
from __future__ import annotations

import argparse
import json
import math
from bisect import bisect_right
from pathlib import Path

from grid.scaling import (
    count_oscillations,
    fit_local_alpha,
    log_spaced,
    usable_window,
)

ROOT = Path(__file__).resolve().parent.parent
TRADES_DIR = ROOT / "data" / "trades"
MONITOR_PATH = ROOT / "data" / "grid_monitor.jsonl"

# 实盘当前运行参数，作为决策的操作点
OPERATING_SPACING = 0.000986
PRICE_TICK = 1.0

# 扫描区间不写死——由 usable_window 从数据现算的 σ 推出。
# 写死绝对格距会在低端落进 α 被压低的伪信号区。
S_POINTS = 15
WINDOW = 5

# 少于这个笔数不做拟合，结论没有统计意义
MIN_TRADES_FOR_FIT = 1000


def segment_by_gap(rows: list[dict]) -> list[list[dict]]:
    """按缺口标记切段。

    跨越缺口拼接会凭空制造一次巨大价格跳变，直接污染振荡计数，因此必须截断。
    少于 2 个点的段无法贡献振荡，丢弃。
    """
    segments: list[list[dict]] = []
    current: list[dict] = []
    for row in rows:
        if row.get("gap"):
            if len(current) >= 2:
                segments.append(current)
            current = []
            continue
        current.append(row)
    if len(current) >= 2:
        segments.append(current)
    return segments


def adx_bucket(adx: float | None) -> str:
    """把 ADX 归入 regime 档位。"""
    if adx is None:
        return "unknown"
    if adx < 20.0:
        return "<20"
    if adx < 30.0:
        return "20-30"
    return ">30"


def load_trades(days: int | None) -> list[dict]:
    """按天读取逐笔数据，保留缺口标记。"""
    if not TRADES_DIR.exists():
        raise SystemExit(f"没有数据目录 {TRADES_DIR}，先把采集器跑起来")
    files = sorted(TRADES_DIR.glob("*.jsonl"))
    if days is not None and days > 0:
        files = files[-days:]
    rows: list[dict] = []
    for path in files:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def load_adx_series() -> list[tuple[float, float]]:
    """从监控 JSONL 读 (秒级时间戳, ADX)，用于 regime 分档。"""
    if not MONITOR_PATH.exists():
        return []
    out = []
    for line in MONITOR_PATH.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if row.get("adx") is not None and row.get("ts") is not None:
            out.append((float(row["ts"]), float(row["adx"])))
    return sorted(out)


def adx_at(series: list[tuple[float, float]], ts_ms: int) -> float | None:
    """取不晚于该时刻的最近一条 ADX。regime 是慢变量，小时级对齐足够。

    用二分而非线性扫描——按 regime 切子段时本函数会被逐行调用。
    """
    if not series:
        return None
    idx = bisect_right(series, (ts_ms / 1000.0, float("inf"))) - 1
    return series[idx][1] if idx >= 0 else None


def split_by_regime(segments: list[list[dict]],
                    adx_series: list[tuple[float, float]]) -> dict[str, list[list[dict]]]:
    """按 ADX 档位把段切成子段。

    一段只在采集断线处才切，可以横跨数小时，期间 ADX 完全可能跨越
    20/30 边界。若只用段首时间戳给整段定档，趋势期的振荡数会被误
    归入震荡档，--by-regime 就失去意义。
    """
    buckets: dict[str, list[list[dict]]] = {}
    for seg in segments:
        current_bucket: str | None = None
        current: list[dict] = []
        for row in seg:
            bucket = adx_bucket(adx_at(adx_series, row["T"]))
            if bucket != current_bucket:
                if len(current) >= 2 and current_bucket is not None:
                    buckets.setdefault(current_bucket, []).append(current)
                current = []
                current_bucket = bucket
            current.append(row)
        if len(current) >= 2 and current_bucket is not None:
            buckets.setdefault(current_bucket, []).append(current)
    return buckets


def segment_sigma(segments: list[list[dict]]) -> float:
    """只用段内相邻收益估计 σ。

    绝不能把各段拼接后再算——段与段之间那对价格是跨越缺口的假收益，
    会把 σ 显著抬高（实测 15 段、段间跳变 0.2% 的场景虚高 438%），
    进而整体平移扫描窗口，甚至把当前操作点排除在窗口之外。
    这与逐段调用 count_oscillations 是同一条纪律。
    """
    rets = [
        math.log(seg[i]["p"] / seg[i - 1]["p"])
        for seg in segments
        for i in range(1, len(seg))
        if seg[i]["p"] > 0 and seg[i - 1]["p"] > 0
    ]
    if len(rets) < 2:
        return 0.0
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    return math.sqrt(var)


def analyze(segments: list[list[dict]], label: str) -> None:
    """对一组段计算振荡数与 α(s) 并打印。"""
    total_points = sum(len(s) for s in segments)
    if total_points < MIN_TRADES_FOR_FIT:
        print(f"\n[{label}] 样本仅 {total_points} 笔，不足以拟合，跳过")
        return

    # σ 由数据现算，窗口随之自动确定——写死绝对格距会落进伪信号区。
    # 只能用段内收益，绝不能拼接后算：段间那对价格是跨越缺口的假收益。
    sigma = segment_sigma(segments)
    ref_price = segments[-1][-1]["p"]  # 用最近的价格，不用最早的
    try:
        low, high = usable_window(sigma, tick=PRICE_TICK, price=ref_price)
    except ValueError as exc:
        print(f"\n[{label}] 无可信扫描区间：{exc}")
        return

    spacings = log_spaced(low, high, S_POINTS)
    counts = [
        sum(count_oscillations([r["p"] for r in seg], s) for seg in segments)
        for s in spacings
    ]

    print(f"\n{'=' * 62}")
    print(f"[{label}] 段数 {len(segments)}，成交 {total_points} 笔")
    print(f"每笔 σ = {sigma * 100:.5f}%，可信窗口 "
          f"[{low * 100:.4f}%, {high * 100:.4f}%]")
    if not low <= OPERATING_SPACING <= high:
        print(f"⚠ 操作点 {OPERATING_SPACING * 100:.4f}% 落在窗口外，"
              f"结论对当前格距不适用，不给出方向性建议")
        return
    print(f"{'格距':>10} {'振荡数':>10}")
    for s, n in zip(spacings, counts):
        print(f"{s * 100:>9.4f}% {n:>10}")

    curve = fit_local_alpha(spacings, counts, window=WINDOW)
    if not curve:
        print("拟合失败（振荡数过少，样本时长可能不够）")
        return

    print(f"\n{'中心格距':>10} {'局部α':>8} {'R²':>7}")
    for center_s, alpha, r2 in curve:
        print(f"{center_s * 100:>9.4f}% {alpha:>8.3f} {r2:>7.3f}")

    center_s, alpha, r2 = min(curve, key=lambda c: abs(c[0] - OPERATING_SPACING))
    op_index = min(range(len(spacings)), key=lambda i: abs(spacings[i] - center_s))
    op_osc = counts[op_index]
    print(f"\n操作点 {OPERATING_SPACING * 100:.4f}% 最近的局部 α = {alpha:.3f} "
          f"(中心 {center_s * 100:.4f}%, R²={r2:.3f})")
    if r2 < 0.9:
        print("⚠ R² < 0.9，幂律假设本身存疑，下述建议不可信")

    if alpha > 2.1:
        print("→ 建议：收窄格距（收益/风险比随 s 减小而上升）")
        if op_osc < 50:
            print(f"  但操作点附近振荡数仅 {op_osc}，绝对收益很低——"
                  f"若同时处于趋势档，正确动作是不做网格而非收窄。")
    elif alpha < 1.9:
        print("→ 建议：放宽格距（收益/风险比随 s 增大而上升）")
    else:
        print("→ 建议：中性，收窄白干。转向加本金或多品种。")


def main() -> None:
    parser = argparse.ArgumentParser(description="格距标度指数 α 分析")
    parser.add_argument("--days", type=int, default=None,
                        help="只分析最近 N 天的文件，默认全部")
    parser.add_argument("--by-regime", action="store_true",
                        help="按 ADX 档位分组分析")
    args = parser.parse_args()

    rows = load_trades(args.days)
    segments = segment_by_gap(rows)
    if not segments:
        raise SystemExit("没有可用数据段")

    analyze(segments, "全部样本")

    if args.by_regime:
        adx_series = load_adx_series()
        buckets = split_by_regime(segments, adx_series)
        for name in ("<20", "20-30", ">30", "unknown"):
            if name in buckets:
                analyze(buckets[name], f"ADX {name}")


if __name__ == "__main__":
    main()
