"""格距标度分析的纯函数集合。

零 I/O、零第三方依赖，全部可单测。设计见
docs/superpowers/specs/2026-08-06-网格收益放大-α测量-design.md
"""
from __future__ import annotations

import math
from collections.abc import Sequence


def count_oscillations(prices: Sequence[float], s: float) -> int:
    """数振幅 >= s 的方向转折次数。

    这是网格真正能吃到的量：卖单在 L(k+1) 只成交一次，要再赚必须让价格
    走满一整格回到 L(k)。相比之下「格线穿越次数」恒等于 离散路径总变差/s，
    对任何路径都给出 α=1，是退化量，不可使用。

    方向未定期（direction == 0）单独处理：此时同时跟踪高低点，
    否则两个分支会互相覆盖极值状态，导致大 s 下恒返回 0。

    Args:
        prices: 按时间排序的价格序列
        s: 相对格距，如 0.000986 表示 0.0986%

    Returns:
        转折次数
    """
    if s <= 0:
        raise ValueError(f"格距须为正：{s}")
    if len(prices) < 2:
        return 0

    count = 0
    direction = 0          # +1 上行，-1 下行，0 未定
    high = low = prices[0]

    for price in prices[1:]:
        if direction == 0:
            if price > high:
                high = price
            if price < low:
                low = price
            if price >= low * (1 + s):
                direction = 1
            elif price <= high * (1 - s):
                direction = -1
        elif direction > 0:
            if price > high:
                high = price
            elif price <= high * (1 - s):
                count += 1
                direction = -1
                low = price
        else:
            if price < low:
                low = price
            elif price >= low * (1 + s):
                count += 1
                direction = 1
                high = price

    return count


# 可用窗口边界（单位：每笔波动 σ 的倍数）。
# 实测依据：布朗路径在 s/σ = 16~64 区间内局部 α 收敛到 1.87~1.98；
# 低于 16 采样相对格距太粗、α 被压低到 1.63（伪信号），
# 高于 64 转折数太少、噪声主导。
SIGMA_MULT_LOW = 16.0
SIGMA_MULT_HIGH = 64.0

# 每格至少要有这么多个价格 tick，否则格号被 tick 离散化扭曲。
MIN_TICKS_PER_GRID = 8


def estimate_sigma(prices: Sequence[float]) -> float:
    """估计相邻样本对数收益的标准差。

    这是定标窗口的尺子——窗口必须相对 σ 而非绝对格距来取，
    否则换个波动率环境就落进伪信号区。

    非正价格一律报错而非静默跳过：静默过滤会让 σ 用更小的样本算出偏小值，
    进而整体平移扫描窗口、毁掉 α，且下游只会看到误导性的「σ 须为正」。
    脏数据必须当场暴露。

    Args:
        prices: 按时间排序的价格序列，全部须为正

    Returns:
        对数收益的样本标准差（n-1 分母）；有效收益不足 2 个时返回 0.0

    Raises:
        ValueError: 价格点少于 2 个，或存在非正价格
    """
    if len(prices) < 2:
        raise ValueError(f"至少需要 2 个价格点，收到 {len(prices)}")
    bad = [i for i, p in enumerate(prices) if p <= 0]
    if bad:
        raise ValueError(
            f"价格序列含 {len(bad)} 个非正值，首个在下标 {bad[0]}（值 {prices[bad[0]]}）"
        )
    rets = [math.log(prices[i] / prices[i - 1]) for i in range(1, len(prices))]
    if len(rets) < 2:
        return 0.0
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    return math.sqrt(var)


def usable_window(sigma: float, tick: float, price: float) -> tuple[float, float]:
    """给出可信的格距扫描区间 [low, high]。

    下界同时受 σ 倍数和 tick 离散化两个约束，取二者较大值。

    Raises:
        ValueError: tick 下界超过 σ 上界时，说明该标的在当前波动率下
            没有可信区间，必须报错而非静默返回空区间。
    """
    if sigma <= 0:
        raise ValueError(f"σ 须为正：{sigma}")
    if tick <= 0 or price <= 0:
        raise ValueError(f"tick 与价格须为正：tick={tick} price={price}")
    tick_floor = MIN_TICKS_PER_GRID * tick / price
    low = max(SIGMA_MULT_LOW * sigma, tick_floor)
    high = SIGMA_MULT_HIGH * sigma
    if low >= high:
        raise ValueError(
            f"无可信区间：下界 {low:.6f} >= 上界 {high:.6f}"
            f"（σ={sigma:.8f}, tick 下界={tick_floor:.6f}）"
        )
    return low, high
