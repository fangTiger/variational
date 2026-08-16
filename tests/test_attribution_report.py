"""归因计算测试。

核心是恒等式自证：权益变动 ≡ 闭环利润 + 未实现盈亏 + 资金费 + 出入金。
两边对不上说明有成交漏记，必须报“归因存在缺口”而不是静默出数——
2026-08-06 的 α 框架就是因为判据无法交叉验证，算错了自己不知道。
"""

from __future__ import annotations

from grid.attribution.report import compute_attribution, verdict


def test_grid_and_directional_split():
    result = compute_attribution(
        equity_start=1000.0,
        equity_end=1040.0,
        loops=[{"gross_pnl": 60.0}],
        funding_total=0.0,
        cash_flow=0.0,
    )
    assert result["grid_pnl"] == 60.0
    assert result["directional_pnl"] == -20.0


def test_cash_flow_excluded():
    """入金不能被算成收益。"""
    result = compute_attribution(
        equity_start=1000.0,
        equity_end=1500.0,
        loops=[{"gross_pnl": 10.0}],
        funding_total=0.0,
        cash_flow=500.0,
    )
    assert result["equity_change"] == 0.0
    assert result["directional_pnl"] == -10.0


def test_residual_within_tolerance_is_clean():
    result = compute_attribution(
        equity_start=1000.0,
        equity_end=1060.0,
        loops=[{"gross_pnl": 60.0}],
        funding_total=0.0,
        cash_flow=0.0,
        unrealised_change=0.0,
    )
    assert result["residual"] == 0.0
    assert result["has_gap"] is False


def test_residual_beyond_tolerance_flags_gap():
    """残差超过当日闭环利润 20% 必须标缺口。"""
    result = compute_attribution(
        equity_start=1000.0,
        equity_end=1200.0,
        loops=[{"gross_pnl": 10.0}],
        funding_total=0.0,
        cash_flow=0.0,
        unrealised_change=0.0,
    )
    assert result["has_gap"] is True


def test_annualised_uses_fixed_principal():
    result = compute_attribution(
        equity_start=1000.0,
        equity_end=1010.0,
        loops=[{"gross_pnl": 10.0}],
        funding_total=0.0,
        cash_flow=0.0,
        days=10.0,
    )
    # 10 天赚 10 元 → 日均 1 元 → 年化 365 元 / 本金 1000 = 36.5%
    assert abs(result["grid_annualised_pct"] - 36.5) < 0.01


def test_verdict_stop_when_below_threshold():
    """闭环年化不足 15% → 停。"""
    result = verdict(
        grid_annualised_pct=14.9,
        equity_annualised_pct=5.0,
        max_drawdown_pct=3.0,
        has_gap=False,
        days=28.0,
    )
    assert result["should_stop"] is True
    assert "闭环年化" in result["reason"]


def test_verdict_stop_when_equity_negative():
    """闭环年化再高，账户净值在亏就该停——闭环年化只是必要不充分条件。"""
    result = verdict(
        grid_annualised_pct=295.0,
        equity_annualised_pct=-3.0,
        max_drawdown_pct=5.0,
        has_gap=False,
        days=28.0,
    )
    assert result["should_stop"] is True
    assert "净值" in result["reason"]


def test_verdict_stop_when_drawdown_too_deep():
    result = verdict(
        grid_annualised_pct=50.0,
        equity_annualised_pct=10.0,
        max_drawdown_pct=12.5,
        has_gap=False,
        days=28.0,
    )
    assert result["should_stop"] is True
    assert "回撤" in result["reason"]


def test_verdict_undecided_before_window_ends():
    """不满 4 周不下结论。"""
    result = verdict(
        grid_annualised_pct=5.0,
        equity_annualised_pct=-10.0,
        max_drawdown_pct=1.0,
        has_gap=False,
        days=10.0,
    )
    assert result["should_stop"] is False
    assert result["decided"] is False


def test_verdict_pass():
    result = verdict(
        grid_annualised_pct=20.0,
        equity_annualised_pct=8.0,
        max_drawdown_pct=4.0,
        has_gap=False,
        days=28.0,
    )
    assert result["should_stop"] is False
    assert result["decided"] is True
