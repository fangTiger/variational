"""Variational swap carry 的无副作用收益计算。"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from engine.swap_trading_schedule import parse_sessions, parse_utc_timestamp


@dataclass(frozen=True)
class CarryReturns:
    """两种持仓政策的年化收益，单位为小数。"""

    weekly_flat: Decimal
    hold_through: Decimal


@dataclass(frozen=True)
class CarryRatios:
    """由真实会话与结算点推导出的周末平仓比例。"""

    hold_ratio: Decimal
    swap_ratio: Decimal
    holding_start: datetime
    holding_end: datetime
    included_settlements: int
    total_settlements: int


def _finite_decimal(value: object, *, label: str) -> Decimal:
    """把输入严格转换为有限 Decimal。"""
    try:
        parsed = Decimal(str(value))
    except (ArithmeticError, TypeError, ValueError) as exc:
        raise ValueError(f"{label} 不是有效十进制数") from exc
    if not parsed.is_finite():
        raise ValueError(f"{label} 必须为有限数")
    return parsed


def _nonnegative_count(value: object, *, label: str) -> int:
    """解析非负整数计数。"""
    if isinstance(value, bool):
        raise ValueError(f"{label} 必须是非负整数")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} 必须是非负整数") from exc
    if parsed < 0 or parsed != value:
        raise ValueError(f"{label} 必须是非负整数")
    return parsed


def calculate_carry_returns(
    *,
    f_perp: Decimal,
    f_swap: Decimal,
    hold_ratio: Decimal,
    perp_accrual_ratio: Decimal,
    swap_ratio: Decimal,
    cost_bps: Decimal = Decimal("6"),
    n_roundtrips: int = 52,
    n_rebalance: int = 4,
) -> CarryReturns:
    """并列计算 weekly-flat 与 hold-through 年化收益。

    ``f_perp``、``f_swap`` 与返回值均为年化小数；例如 10.2888% 传
    ``Decimal("0.102888")``。``perp_accrual_ratio`` 是 RWA 永续费率实际
    计提的时段占比，独立于 ``hold_ratio`` 的持仓占比。成本输入单位是 bp，
    函数内部换算为小数。
    """
    perp = _finite_decimal(f_perp, label="永续费率")
    swap = _finite_decimal(f_swap, label="swap 费率")
    holding = _finite_decimal(hold_ratio, label="weekly-flat 永续持仓比例")
    # hold-through 虽然持续持仓，但 RWA 永续仅在标的现货开市时计息；
    # 该占空比与 weekly-flat 的持仓占比是两个独立经济量。
    perp_accrual = _finite_decimal(
        perp_accrual_ratio, label="hold-through 永续计息占空比"
    )
    swap_accrual = _finite_decimal(swap_ratio, label="swap 计息比例")
    cost = _finite_decimal(cost_bps, label="往返成本 bp") / Decimal("10000")
    roundtrips = _nonnegative_count(n_roundtrips, label="年往返次数")
    rebalances = _nonnegative_count(n_rebalance, label="年再平衡次数")
    if not Decimal(0) <= holding <= Decimal(1):
        raise ValueError("weekly-flat 永续持仓比例必须在 0 到 1 之间")
    if not Decimal(0) <= perp_accrual <= Decimal(1):
        raise ValueError("hold-through 永续计息占空比必须在 0 到 1 之间")
    if not Decimal(0) <= swap_accrual <= Decimal(1):
        raise ValueError("swap 计息比例必须在 0 到 1 之间")
    if cost < 0:
        raise ValueError("往返成本 bp 不能为负数")

    return CarryReturns(
        weekly_flat=(
            perp * holding - swap * swap_accrual - Decimal(roundtrips) * cost
        ),
        hold_through=(
            perp * perp_accrual - swap - Decimal(rebalances) * cost
        ),
    )


def _timedelta_seconds(value: timedelta) -> Decimal:
    """无浮点损失地把 timedelta 转成 Decimal 秒数。"""
    whole_seconds = value.days * 86400 + value.seconds
    return Decimal(whole_seconds) + Decimal(value.microseconds) / Decimal("1000000")


def _settlement_datetime(value: object, *, index: int) -> datetime:
    """解析一个实际结算时点。"""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError(f"settlement_points[{index}] 必须带时区")
        return value.astimezone(timezone.utc)
    return parse_utc_timestamp(value, label=f"settlement_points[{index}]")


def derive_perp_accrual_ratio(
    trading_sessions: object,
    *,
    period: timedelta,
) -> Decimal:
    """从真实交易会话累计 RWA 永续的计息占空比。

    该比例只描述标的现货开市、平台费率有效的时间，不描述策略是否持仓；
    因而不能复用 weekly-flat 的连续持仓窗口比例。
    """
    sessions = parse_sessions(trading_sessions)
    if period <= timedelta(0):
        raise ValueError("计息观察周期必须大于零")
    accrued = sum(
        (session.close_at - session.open_at for session in sessions),
        timedelta(0),
    )
    if accrued > period:
        raise ValueError("交易会话总时长不能长于计息观察周期")
    return _timedelta_seconds(accrued) / _timedelta_seconds(period)


def derive_carry_ratios(
    trading_sessions: object,
    settlement_points: Sequence[object],
    *,
    pre_close_buffer: timedelta,
    period: timedelta,
) -> CarryRatios:
    """从真实会话边界与实际结算点推导 weekly-flat 两腿比例。

    持仓窗口从输入中的最早开市开始，到最晚收市减去平仓缓冲结束；期间的
    日内短暂停市不会自动平仓，所以永续腿按整个连续窗口计时。swap 比例只
    统计真正落在该持仓窗口内的结算点，不假设固定的每日时钟。
    """
    sessions = parse_sessions(trading_sessions)
    if not isinstance(settlement_points, Sequence) or isinstance(
        settlement_points, (str, bytes)
    ):
        raise ValueError("settlement_points 必须是数组")
    if not settlement_points:
        raise ValueError("settlement_points 为空")
    if pre_close_buffer < timedelta(0):
        raise ValueError("平仓缓冲不能为负数")
    if period <= timedelta(0):
        raise ValueError("收益观察周期必须大于零")

    holding_start = min(session.open_at for session in sessions)
    holding_end = max(session.close_at for session in sessions) - pre_close_buffer
    if holding_end <= holding_start:
        raise ValueError("平仓缓冲使持仓窗口为空")
    duration = holding_end - holding_start
    if duration > period:
        raise ValueError("持仓窗口不能长于收益观察周期")

    points = tuple(
        _settlement_datetime(value, index=index)
        for index, value in enumerate(settlement_points)
    )
    included = sum(holding_start <= point <= holding_end for point in points)
    return CarryRatios(
        hold_ratio=_timedelta_seconds(duration) / _timedelta_seconds(period),
        swap_ratio=Decimal(included) / Decimal(len(points)),
        holding_start=holding_start,
        holding_end=holding_end,
        included_settlements=included,
        total_settlements=len(points),
    )
