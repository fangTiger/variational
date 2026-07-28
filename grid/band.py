"""有界区间 band 纯函数（固定不追价，见设计 v3 Component 2）。"""
from __future__ import annotations


def compute_band(price: float, atr: float, k: float, min_half_frac: float) -> tuple[float, float]:
    """区间 = price ± max(k*atr, min_half_frac*price)。返回 (low, high)。

    min_half_frac 保证低波动时半宽不至于窄到只能挂一格。
    """
    half = max(k * atr, min_half_frac * price)
    return price - half, price + half


def is_out_of_band(price: float, low: float, high: float) -> bool:
    return price < low or price > high


def blocked_side_for_breach(price: float, low: float, high: float) -> str | None:
    """越界后应冻结（停止新增）的方向。跌破下界→冻结 BUY；涨破上界→冻结 SELL。"""
    if price < low:
        return "BUY"
    if price > high:
        return "SELL"
    return None
