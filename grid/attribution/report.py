"""归因计算与去留判据。纯函数，无 IO。"""

from __future__ import annotations

# 残差超过当日闭环利润这个比例，就认为归因有缺口
_RESIDUAL_TOLERANCE_FRAC = 0.20
# 残差绝对值下限：闭环利润接近 0 时，比例判据会过于敏感
_RESIDUAL_TOLERANCE_ABS = 1.0

OBSERVATION_DAYS = 28.0
MIN_GRID_ANNUALISED_PCT = 15.0
MAX_DRAWDOWN_PCT = 12.0


def compute_attribution(
    *,
    equity_start: float,
    equity_end: float,
    loops: list[dict],
    funding_total: float,
    cash_flow: float,
    unrealised_change: float | None = None,
    days: float = 1.0,
) -> dict:
    """把权益变动拆成网格闭环利润与方向性盈亏。

    恒等式：权益变动 ≡ 闭环利润 + 未实现盈亏变动 + 资金费 + 出入金
    出入金先从权益变动里剔除，否则一笔入金会被算成收益。
    """
    equity_change = equity_end - equity_start - cash_flow
    grid_pnl = sum(float(loop["gross_pnl"]) for loop in loops)
    directional_pnl = equity_change - grid_pnl - funding_total

    residual = 0.0
    if unrealised_change is not None:
        expected = grid_pnl + unrealised_change + funding_total
        residual = equity_change - expected

    tolerance = max(
        abs(grid_pnl) * _RESIDUAL_TOLERANCE_FRAC,
        _RESIDUAL_TOLERANCE_ABS,
    )
    principal = equity_start if equity_start > 0 else 1.0
    grid_annualised_pct = (
        (grid_pnl / max(days, 1e-9)) * 365.0 / principal * 100.0
    )

    return {
        "equity_change": equity_change,
        "grid_pnl": grid_pnl,
        "directional_pnl": directional_pnl,
        "funding_total": funding_total,
        "residual": residual,
        "has_gap": abs(residual) > tolerance,
        "grid_annualised_pct": grid_annualised_pct,
        "days": days,
    }


def verdict(
    *,
    grid_annualised_pct: float,
    equity_annualised_pct: float,
    max_drawdown_pct: float,
    has_gap: bool,
    days: float,
) -> dict:
    """4 周去留判据。三方评审一致：闭环年化只是必要不充分条件。"""
    if days < OBSERVATION_DAYS:
        return {
            "decided": False,
            "should_stop": False,
            "reason": f"观察期未满（{days:.1f}/{OBSERVATION_DAYS:.0f} 天）",
        }
    reasons = []
    if grid_annualised_pct < MIN_GRID_ANNUALISED_PCT:
        reasons.append(
            f"闭环年化 {grid_annualised_pct:.1f}% < "
            f"{MIN_GRID_ANNUALISED_PCT:.0f}%"
        )
    if equity_annualised_pct <= 0:
        reasons.append(f"净值年化 {equity_annualised_pct:.1f}% ≤ 0")
    if max_drawdown_pct >= MAX_DRAWDOWN_PCT:
        reasons.append(
            f"最大回撤 {max_drawdown_pct:.1f}% ≥ {MAX_DRAWDOWN_PCT:.0f}%"
        )
    if has_gap:
        reasons.append("归因存在缺口，数据不可信")
    return {
        "decided": True,
        "should_stop": bool(reasons),
        "reason": "；".join(reasons) if reasons else "全部判据通过",
    }
