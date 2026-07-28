"""有界区间 band 纯函数测试：最小半宽、越界判断、冻结方向推导。"""
from __future__ import annotations

from grid.band import blocked_side_for_breach, compute_band, is_out_of_band


def test_compute_band_symmetric() -> None:
    lo, hi = compute_band(price=65000.0, atr=500.0, k=1.75, min_half_frac=0.0)
    assert lo == 65000.0 - 1.75 * 500.0
    assert hi == 65000.0 + 1.75 * 500.0


def test_min_half_width_floor() -> None:
    # k*atr 只有 0.5%，min_half_frac=0.04（4%）应把半宽撑到 4%
    lo, hi = compute_band(price=100.0, atr=0.5, k=1.75, min_half_frac=0.04)
    assert hi - 100.0 == 4.0  # 撑到 4%
    assert 100.0 - lo == 4.0


def test_out_of_band() -> None:
    assert is_out_of_band(59.0, low=60.0, high=70.0) is True   # 跌破下界
    assert is_out_of_band(71.0, low=60.0, high=70.0) is True   # 涨破上界
    assert is_out_of_band(65.0, low=60.0, high=70.0) is False


def test_blocked_side_for_breach() -> None:
    # 跌破下界 → 亏损的是多头，冻结 BUY（停止继续买）
    assert blocked_side_for_breach(59.0, low=60.0, high=70.0) == "BUY"
    # 涨破上界 → 亏损的是空头，冻结 SELL
    assert blocked_side_for_breach(71.0, low=60.0, high=70.0) == "SELL"
    assert blocked_side_for_breach(65.0, low=60.0, high=70.0) is None
