"""硬止损纯判定（距强平价百分比，见设计 v3 Component 3）。"""
from __future__ import annotations


def dist_to_liq_pct(mark: float, liq: float, signed_size: float) -> float:
    """距强平价的相对距离（0~1）。空仓返回 inf（永不触发）。"""
    if signed_size == 0 or mark <= 0:
        return float("inf")
    if signed_size > 0:            # 多头，liq 在下
        return (mark - liq) / mark
    return (liq - mark) / mark     # 空头，liq 在上


def hard_stop_triggered(mark: float, liq: float, signed_size: float, stop_dist: float) -> bool:
    """距强平价 < stop_dist 即触发。空仓永不触发。"""
    return dist_to_liq_pct(mark, liq, signed_size) < stop_dist
