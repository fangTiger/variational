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
                high = price
            elif price <= high * (1 - s):
                direction = -1
                low = price
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
