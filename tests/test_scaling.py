"""振荡计数纯函数测试。

度量的是「振幅 >= s 的方向转折次数」，不是「格线穿越次数」——
后者恒等于 α=1，是退化量。
"""
from __future__ import annotations

import math

from grid.scaling import (
    SIGMA_MULT_HIGH,
    SIGMA_MULT_LOW,
    count_oscillations,
    estimate_sigma,
    fit_local_alpha,
    log_spaced,
    usable_window,
)


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


def test_estimate_sigma_matches_known_value() -> None:
    # 构造对数收益恒为 ±0.001 的序列，标准差应为 0.001
    prices = [100.0]
    for i in range(200):
        prices.append(prices[-1] * math.exp(0.001 if i % 2 == 0 else -0.001))
    sigma = estimate_sigma(prices)
    assert abs(sigma - 0.001) < 1e-4


def test_estimate_sigma_zero_for_flat() -> None:
    assert estimate_sigma([100.0] * 50) == 0.0


def test_estimate_sigma_needs_two_points() -> None:
    import pytest
    with pytest.raises(ValueError):
        estimate_sigma([100.0])


def test_usable_window_scales_with_sigma() -> None:
    low, high = usable_window(sigma=0.00003, tick=1.0, price=64700.0)
    assert abs(low - SIGMA_MULT_LOW * 0.00003) < 1e-12
    assert abs(high - SIGMA_MULT_HIGH * 0.00003) < 1e-12


def test_usable_window_respects_tick_floor() -> None:
    # σ=4e-6 时 16σ=6.4e-5 低于 tick 下界 1.237e-4，下界应被顶上去；
    # 同时 64σ=2.56e-4 仍高于下界，区间有效。
    # 注意不能取过小的 σ（如 1e-9），那会触发下面的「无可信区间」分支。
    low, high = usable_window(sigma=4e-6, tick=1.0, price=64700.0)
    assert abs(low - 8 * 1.0 / 64700.0) < 1e-12
    assert low < high


def test_usable_window_rejects_inverted_range() -> None:
    import pytest
    # σ 太小导致 tick 下界超过上界时必须报错而非静默返回空区间
    with pytest.raises(ValueError):
        usable_window(sigma=1e-12, tick=1.0, price=100.0)


def test_estimate_sigma_rejects_non_positive_price() -> None:
    """脏数据必须当场报错，不能静默过滤成偏小的 σ。

    回归：[100.0, -5.0, 102.0] 原会静默返回 0.0，下游只看到误导性的
    「σ 须为正」，真正病因（价格损坏）被掩盖。
    """
    import pytest
    with pytest.raises(ValueError, match="非正值"):
        estimate_sigma([100.0, -5.0, 102.0])
    with pytest.raises(ValueError, match="非正值"):
        estimate_sigma([100.0, 0.0])


def test_estimate_sigma_single_return_yields_zero() -> None:
    # 两个价格点只有一个收益，算不出样本标准差，返回 0.0
    assert estimate_sigma([100.0, 105.0]) == 0.0


def test_usable_window_rejects_non_positive_sigma() -> None:
    import pytest
    with pytest.raises(ValueError, match="σ 须为正"):
        usable_window(sigma=0.0, tick=1.0, price=100.0)
    with pytest.raises(ValueError, match="σ 须为正"):
        usable_window(sigma=-1e-5, tick=1.0, price=100.0)


def test_usable_window_rejects_non_positive_tick_or_price() -> None:
    import pytest
    with pytest.raises(ValueError, match="tick 与价格须为正"):
        usable_window(sigma=3e-5, tick=0.0, price=100.0)
    with pytest.raises(ValueError, match="tick 与价格须为正"):
        usable_window(sigma=3e-5, tick=1.0, price=0.0)


def test_log_spaced_endpoints_and_count() -> None:
    pts = log_spaced(0.0002, 0.004, 15)
    assert len(pts) == 15
    assert abs(pts[0] - 0.0002) < 1e-12
    assert abs(pts[-1] - 0.004) < 1e-12
    # 对数均匀：相邻比值恒定
    ratios = [pts[i + 1] / pts[i] for i in range(len(pts) - 1)]
    assert max(ratios) - min(ratios) < 1e-9


def test_log_spaced_validates_inputs() -> None:
    import pytest
    with pytest.raises(ValueError):
        log_spaced(0.0, 0.004, 15)          # 下界非正
    with pytest.raises(ValueError):
        log_spaced(0.004, 0.0002, 15)       # 上下界颠倒
    with pytest.raises(ValueError):
        log_spaced(0.0002, 0.004, 1)        # 点数不足


def test_fit_local_alpha_recovers_exact_power_law() -> None:
    # 构造 N(s) = C * s^(-2)，局部斜率处处应为 2.0
    spacings = log_spaced(0.0002, 0.004, 15)
    counts = [round(1e6 * s ** -2.0) for s in spacings]
    curve = fit_local_alpha(spacings, counts, window=5)
    assert len(curve) == 11
    for _s, alpha, r2 in curve:
        assert abs(alpha - 2.0) < 0.05
        assert r2 > 0.99


def test_fit_local_alpha_recovers_slope_one() -> None:
    # N(s) = C * s^(-1) → α = 1.0
    spacings = log_spaced(0.0002, 0.004, 15)
    counts = [round(1e4 * s ** -1.0) for s in spacings]
    curve = fit_local_alpha(spacings, counts, window=5)
    for _s, alpha, _r2 in curve:
        assert abs(alpha - 1.0) < 0.05


def test_fit_local_alpha_center_is_window_midpoint() -> None:
    # 返回的第一个中心格距应是窗口中点（window=5 → 下标 2）
    spacings = log_spaced(0.0002, 0.004, 15)
    counts = [round(1e6 * s ** -2.0) for s in spacings]
    curve = fit_local_alpha(spacings, counts, window=5)
    assert abs(curve[0][0] - spacings[2]) < 1e-12


def test_fit_local_alpha_skips_zero_counts() -> None:
    # 零穿越点取不了对数，必须被跳过而不是崩溃
    spacings = log_spaced(0.0002, 0.004, 15)
    counts = [0] * 10 + [100, 80, 60, 40, 20]
    curve = fit_local_alpha(spacings, counts, window=5)
    assert isinstance(curve, list)
    assert len(curve) == 1


def test_fit_local_alpha_requires_enough_points() -> None:
    assert fit_local_alpha([0.001, 0.002], [100, 50], window=5) == []
