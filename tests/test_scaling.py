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
