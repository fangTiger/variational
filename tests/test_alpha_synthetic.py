"""合成路径验证：算法能否正确还原已知的标度指数。

这是整套测量的闸门。历史上两次离线 A/B 因建模错误作废，此处用参数已知的
合成路径把算法本身钉死——不通过则不得使用实盘数据。

容差取自用真实 grid/scaling.py 的实测值，不得为了让测试通过而调宽。

本闸门只保证**宏观标度指数**正确，不保证 count_oscillations 的每个分支正确。
变异测试实证：把「方向确立时覆盖真实极值」这个已修复的 Critical bug 复现回去，
本文件 4 条测试全绿、α 小数点后 4 位不变——因为该 bug 只影响每条路径一次性的
初始方向确立，被 20 万步的统计聚合稀释到噪声以下。

它由 tests/test_scaling.py 的短序列单元测试守护（尤其是
test_initial_extreme_survives_direction_commit）。那些测试是本闸门的必要补充，
不可删除或弱化。
"""
from __future__ import annotations

import math
import random

from grid.scaling import (
    count_oscillations,
    estimate_sigma,
    fit_local_alpha,
    log_spaced,
    usable_window,
)

S_POINTS = 15
WINDOW = 5
# 合成路径没有真实 tick 概念，取极小值让窗口只受 σ 约束
SYNTHETIC_TICK = 1e-9


def _make_path(steps: int, sigma: float, drift: float, seed: int) -> list[float]:
    """生成几何布朗运动路径（对数收益为独立正态）。"""
    rng = random.Random(seed)
    price = 100.0
    out = [price]
    for _ in range(steps):
        price *= math.exp(drift + rng.gauss(0.0, sigma))
        out.append(price)
    return out


def _alpha_stats(prices: list[float]) -> tuple[float, float, list[int]]:
    """返回 (局部 α 均值, 最低 R², 各格距的转折数)。"""
    sigma = estimate_sigma(prices)
    low, high = usable_window(sigma, tick=SYNTHETIC_TICK, price=prices[0])
    spacings = log_spaced(low, high, S_POINTS)
    counts = [count_oscillations(prices, s) for s in spacings]
    curve = fit_local_alpha(spacings, counts, window=WINDOW)
    assert curve, "拟合结果为空"
    alphas = [a for _s, a, _r2 in curve]
    return sum(alphas) / len(alphas), min(r2 for _s, _a, r2 in curve), counts


def test_estimate_sigma_recovers_true_value() -> None:
    """σ 估计准确是窗口正确的前提。实测 0.00020022（真值 0.0002）。"""
    prices = _make_path(steps=50_000, sigma=0.0002, drift=0.0, seed=42)
    assert abs(estimate_sigma(prices) - 0.0002) < 1e-5


def test_brownian_motion_gives_alpha_near_two() -> None:
    """无漂移布朗运动：局部 α 必须落在 2.0 附近。

    这一关不过说明振荡计数或拟合本身有误，整套测量作废。
    三个种子实测 α = 1.9524 / 2.0685 / 2.0278，最低 R² 0.9678。
    """
    for seed in (42, 7, 2026):
        mean_alpha, min_r2, _counts = _alpha_stats(
            _make_path(steps=200_000, sigma=0.0002, drift=0.0, seed=seed)
        )
        assert 1.75 < mean_alpha < 2.25, f"seed={seed} α={mean_alpha:.4f}，应≈2"
        assert min_r2 > 0.9, f"seed={seed} R²={min_r2:.4f} 过低"


def test_trending_path_gives_alpha_above_two() -> None:
    """强漂移路径：局部 α 必须显著高于 2。

    方向易记反——趋势把振荡消灭得比扩散标度更快，所以 α 上升而非下降。
    漂移 3e-6 实测 α = 2.3908。
    """
    mean_alpha, _min_r2, _counts = _alpha_stats(
        _make_path(steps=200_000, sigma=0.0002, drift=3e-6, seed=7)
    )
    assert mean_alpha > 2.2, f"趋势路径 α={mean_alpha:.4f}，应明显>2"


def test_oscillation_count_decreases_with_spacing() -> None:
    """基本单调性：格距越宽转折越少。5 个种子实测全部成立。"""
    for seed in (1, 42, 7, 2026, 99):
        _mean, _r2, counts = _alpha_stats(
            _make_path(steps=50_000, sigma=0.0002, drift=0.0, seed=seed)
        )
        for i in range(len(counts) - 1):
            assert counts[i] >= counts[i + 1], f"seed={seed} 第{i}点非单调：{counts}"
