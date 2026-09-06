"""Swap carry 两种政策收益模型测试。"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from engine.swap_carry import (
    calculate_carry_returns,
    derive_carry_ratios,
    derive_perp_accrual_ratio,
)


def test_plan_example_calculates_both_policies_side_by_side() -> None:
    """计息占空比修正后，两种政策应得到约 0.86% 与 1.29%。"""
    result = calculate_carry_returns(
        f_perp=Decimal("0.102888"),
        f_swap=Decimal("0.057214"),
        hold_ratio=Decimal("0.705"),
        perp_accrual_ratio=Decimal("0.705"),
        swap_ratio=Decimal(4) / Decimal(7),
        cost_bps=Decimal("6"),
        n_roundtrips=52,
        n_rebalance=4,
    )

    assert abs(result.weekly_flat - Decimal("0.0086")) < Decimal("0.0001")
    assert abs(result.hold_through - Decimal("0.01292204")) < Decimal("0.0001")


def test_perpetual_and_swap_ratios_are_applied_separately() -> None:
    """模型不得先求净 carry 再统一乘一个持仓系数。"""
    result = calculate_carry_returns(
        f_perp=Decimal("0.10"),
        f_swap=Decimal("0.05"),
        hold_ratio=Decimal("0.50"),
        perp_accrual_ratio=Decimal("0.75"),
        swap_ratio=Decimal("0.25"),
        cost_bps=Decimal("0"),
        n_roundtrips=0,
        n_rebalance=0,
    )

    assert result.weekly_flat == Decimal("0.0375")
    assert result.weekly_flat != (Decimal("0.10") - Decimal("0.05")) * Decimal(
        "0.50"
    )


def test_ratios_are_derived_from_sessions_and_actual_settlement_points() -> None:
    """周末平仓比例由真实会话边界和实际结算点推导。"""
    sessions = [
        {"open": "2026-09-06T22:00:00Z", "close": "2026-09-07T21:00:00Z"},
        {"open": "2026-09-07T22:00:00Z", "close": "2026-09-08T21:00:00Z"},
        {"open": "2026-09-08T22:00:00Z", "close": "2026-09-09T21:00:00Z"},
        {"open": "2026-09-09T22:00:00Z", "close": "2026-09-10T21:00:00Z"},
        {"open": "2026-09-10T22:00:00Z", "close": "2026-09-11T21:00:00Z"},
    ]
    settlement_points = [f"2026-09-{day:02d}T21:05:00Z" for day in range(6, 13)]

    ratios = derive_carry_ratios(
        sessions,
        settlement_points,
        pre_close_buffer=timedelta(minutes=30),
        period=timedelta(days=7),
    )

    assert ratios.holding_start.isoformat() == "2026-09-06T22:00:00+00:00"
    assert ratios.holding_end.isoformat() == "2026-09-11T20:30:00+00:00"
    assert ratios.hold_ratio == Decimal(1185) / Decimal(1680)
    assert ratios.swap_ratio == Decimal(4) / Decimal(7)
    assert ratios.included_settlements == 4
    assert ratios.total_settlements == 7


def test_perp_accrual_ratio_is_derived_from_real_session_durations() -> None:
    """计息占空比应累计真实开市时长，不能复用连续持仓窗口比例。"""
    sessions = [
        {"open": "2026-09-06T22:00:00Z", "close": "2026-09-07T21:00:00Z"},
        {"open": "2026-09-07T22:00:00Z", "close": "2026-09-08T21:00:00Z"},
        {"open": "2026-09-08T22:00:00Z", "close": "2026-09-09T21:00:00Z"},
        {"open": "2026-09-09T22:00:00Z", "close": "2026-09-10T21:00:00Z"},
        {"open": "2026-09-10T22:00:00Z", "close": "2026-09-11T21:00:00Z"},
    ]

    result = derive_perp_accrual_ratio(sessions, period=timedelta(days=7))

    assert result == Decimal(115) / Decimal(168)
    assert result != Decimal("0.705")
