"""资金费归一化与方向选择测试。"""

from __future__ import annotations

from decimal import Decimal

from grid.grid_state import GridState, save_state
from tracking import monitor
from tracking.monitor import compute_funding_view
from tools import grid_monitor, run_grid


def test_funding_normalization_and_direction() -> None:
    """Variational 预测值按年化小数换算后，再与 Extended 的 8h 口径比较。"""
    # 0.066721 是 /funding/v2 的真实量级：正确含义为年化 6.6721%。
    v = compute_funding_view(Decimal("0.066721"), Decimal("0.000013"))
    assert v.var_pct_8h * Decimal("1095") == Decimal("6.672100")
    # Extended 算法暂时保持原样：0.000013 * 100 * 8 = 0.0104 %/8h。
    assert abs(v.ext_pct_8h - Decimal("0.0104")) < Decimal("1e-9")
    # 新口径下 Extended 费率更高，方向应翻转为做多 Variational。
    assert "做多" in v.recommended and "Variational" in v.recommended
    assert v.carry_short_var_pct_8h < 0
    assert v.annualized_pct == Decimal("4.715900")
    assert "折算 %/8h" in v.pretty()


def test_rejects_old_variational_percent_per_period_interpretation() -> None:
    """拒绝把 0.066721 误读成 0.066721%/8h 的旧口径。"""
    predicted = Decimal("0.066721")
    view = compute_funding_view(predicted, Decimal("0"))

    # 旧算法会得到 73.059495% 年化，远离历史已结算约 2.5%~11% 的区间。
    old_annualized_pct = predicted * Decimal("1095")
    assert old_annualized_pct == Decimal("73.059495")
    assert view.var_pct_8h != predicted
    # 新算法把原值直接视为年化小数，即年化 6.6721%。
    assert view.var_pct_8h * Decimal("1095") == Decimal("6.672100")


def test_direction_flips_when_extended_higher() -> None:
    """Extended 资金费更高时应推荐反向。"""
    v = compute_funding_view(Decimal("0.001"), Decimal("0.0005"))
    # ext: 0.0005*100*8 = 0.4 %/8h > var 0.001 → 推荐 Variational 做多
    assert v.carry_short_var_pct_8h < 0
    assert "做多" in v.recommended and "Variational" in v.recommended
    assert v.annualized_pct > 0  # 推荐方向年化应为正


def test_extended_funding_rate_is_visibly_uncalibrated() -> None:
    """任何资金费视图都必须明确标出 Extended 单位尚未经结算校准。"""
    view = compute_funding_view(Decimal("0.066721"), Decimal("0.000013"))

    assert view.extended_calibrated is False
    assert "Extended" in view.pretty()
    assert "未经校准" in view.pretty()


def test_direction_flip_produces_warning(monkeypatch) -> None:
    """连续观测的推荐方向翻转时不得静默改变建议。"""
    monkeypatch.setattr(monitor, "_last_recommended_direction", None, raising=False)

    first = compute_funding_view(Decimal("0.10"), Decimal("0"))
    second = compute_funding_view(Decimal("0"), Decimal("0.00001"))

    assert "Variational 做空" in first.recommended
    assert "Variational 做多" in second.recommended
    assert any("方向翻转" in warning for warning in second.warnings)
    assert "⚠️" in second.pretty()


def test_close_funding_rates_produce_unstable_direction_warning(monkeypatch) -> None:
    """两腿折算费率相同或接近时必须提示方向不稳健。"""
    monkeypatch.setattr(monitor, "_last_recommended_direction", None, raising=False)

    view = compute_funding_view(Decimal("0.00876"), Decimal("0.000001"))

    assert any("不稳健" in warning for warning in view.warnings)


def test_run_grid_trend_aware_cli_defaults_and_overrides() -> None:
    """趋势感知 CLI 参数应有安全默认值，并完整透传给 GridConfig。"""
    parser = run_grid._build_parser()
    defaults = parser.parse_args([])
    assert defaults.trend_aware is False
    assert defaults.band_k == 1.75
    assert defaults.min_half_frac == 0.04
    assert defaults.max_drawdown is None
    assert defaults.hard_stop_dist is None
    default_config = run_grid._grid_config(defaults)
    assert default_config.max_drawdown_pct == 0.12
    assert default_config.hard_stop_dist == 0.12
    assert default_config.explicit_risk_flags == ()

    args = parser.parse_args(
        [
            "--trend-aware",
            "--band-k",
            "2.25",
            "--min-half-frac",
            "0.047",
            "--hard-stop-dist",
            "0.08",
        ]
    )
    config = run_grid._grid_config(args)
    assert config.trend_aware is True
    assert config.band_k == 2.25
    assert config.min_half_frac == 0.047
    assert config.hard_stop_dist == 0.08
    assert config.max_drawdown_pct == 0.12
    assert config.explicit_risk_flags == (
        "--trend-aware",
        "--hard-stop-dist",
    )


def test_read_grid_state_missing_returns_none_fields(tmp_path) -> None:
    """状态文件缺失时监控字段全部为 None，且不抛异常。"""
    fields = grid_monitor._read_grid_state(tmp_path / "grid_state.json")
    assert fields == {
        "frozen": None,
        "blocked_side": None,
        "halted": None,
        "band_low": None,
        "band_high": None,
    }


def test_read_grid_state_returns_persisted_fields(tmp_path) -> None:
    """监控直接复用持久化状态，不重新推导趋势分类。"""
    path = tmp_path / "grid_state.json"
    save_state(
        path,
        GridState(
            band_low=63000.0,
            band_high=68000.0,
            frozen=True,
            blocked_side="BUY",
            halted=False,
        ),
    )

    assert grid_monitor._read_grid_state(path) == {
        "frozen": True,
        "blocked_side": "BUY",
        "halted": False,
        "band_low": 63000.0,
        "band_high": 68000.0,
    }


def test_liquidation_distance_is_safe_for_flat_position() -> None:
    """空仓查询不到强平信息时返回 None；有仓时复用 risk 纯函数。"""
    assert grid_monitor._liquidation_distance_pct(None, signed_size=0.0) is None
    assert (
        grid_monitor._liquidation_distance_pct(
            (Decimal("100"), Decimal("90")),
            signed_size=1.0,
        )
        == 0.10
    )


def test_grid_report_prints_trend_state_and_liquidation_distance(
    monkeypatch, capsys
) -> None:
    """历史报告应展示持久化趋势状态、band 与距强平百分比。"""
    monkeypatch.setattr(
        grid_monitor,
        "_load",
        lambda: [
            {
                "ts": 1.0,
                "alive": True,
                "equity": 300.0,
                "pnl_since_start": 5.0,
                "inv_btc": 0.01,
                "inv_usd": 650.0,
                "price": 65000.0,
                "adx": 18.5,
                "mode": "neutral",
                "frozen": True,
                "blocked_side": "BUY",
                "halted": False,
                "band_low": 63000.0,
                "band_high": 68000.0,
                "dist_to_liq_pct": 0.10,
            }
        ],
    )

    grid_monitor._report()

    output = capsys.readouterr().out
    assert "frozen=True" in output
    assert "blocked_side=BUY" in output
    assert "halted=False" in output
    assert "band=[63000, 68000]" in output
    assert "距强平 10.0%" in output


if __name__ == "__main__":
    test_funding_normalization_and_direction()
    test_direction_flips_when_extended_higher()
    print("✅ monitor 测试通过")
