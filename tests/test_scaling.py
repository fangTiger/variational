"""振荡计数纯函数测试。

度量的是「振幅 >= s 的方向转折次数」，不是「格线穿越次数」——
后者恒等于 α=1，是退化量。
"""
from __future__ import annotations

from grid.scaling import count_oscillations


def test_no_oscillation_when_flat() -> None:
    assert count_oscillations([100.0, 100.0, 100.0], s=0.01) == 0


def test_no_oscillation_on_monotone_rise() -> None:
    # 一路上涨没有转折，网格吃不到闭环
    assert count_oscillations([100.0, 105.0, 110.0, 120.0], s=0.01) == 0


def test_single_reversal_counts_once() -> None:
    # 涨到 110 后跌破 110*(1-0.01)=108.9 → 一次转折
    assert count_oscillations([100.0, 110.0, 108.0], s=0.01) == 1


def test_reversal_below_threshold_not_counted() -> None:
    # 只回落到 109.5，未跌破 108.9，不算转折
    assert count_oscillations([100.0, 110.0, 109.5], s=0.01) == 0


def test_round_trip_counts_two_turns() -> None:
    # 涨 → 跌破一格（1 次）→ 再涨满一格（2 次）
    prices = [100.0, 110.0, 108.0, 110.0]
    assert count_oscillations(prices, s=0.01) == 2


def test_intra_grid_noise_ignored() -> None:
    # 全程波动幅度小于 s，零转折
    prices = [100.0, 100.3, 100.1, 100.4, 100.2]
    assert count_oscillations(prices, s=0.01) == 0


def test_wider_spacing_never_yields_more_oscillations() -> None:
    # 单调性：格距越宽转折越少（或持平）
    prices = [100.0, 103.0, 99.0, 104.0, 98.0, 105.0]
    counts = [count_oscillations(prices, s=s)
              for s in (0.005, 0.01, 0.02, 0.04, 0.08)]
    for i in range(len(counts) - 1):
        assert counts[i] >= counts[i + 1], counts


def test_short_series() -> None:
    assert count_oscillations([], s=0.01) == 0
    assert count_oscillations([100.0], s=0.01) == 0


def test_validates_spacing() -> None:
    import pytest
    with pytest.raises(ValueError):
        count_oscillations([100.0, 101.0], s=0.0)


def test_initial_extreme_survives_direction_commit() -> None:
    """方向确立时不得丢掉未定阶段追踪到的真实极值。

    回归用例：200→181 跌 9.5% 未确立方向；181→199.5 涨 10.2% 确立上行。
    此时若把 high 从真实峰值 200 覆盖成 199.5，后续跌到 179.8（距 200 已
    跌 10.1%）就会被漏计。
    """
    assert count_oscillations([200.0, 181.0, 199.5, 179.8], s=0.10) == 1


def test_initial_downward_move_establishes_direction() -> None:
    # 先跌破一格确立下行，再涨满一格记一次转折
    assert count_oscillations([100.0, 90.0, 102.0], s=0.05) == 1


def test_extend_low_before_reversal() -> None:
    # 下行途中不断刷新新低，最后反弹满一格才记转折
    assert count_oscillations([100.0, 110.0, 108.0, 105.0, 110.0], s=0.01) == 2
