"""资金费归一化与方向选择测试。"""

from __future__ import annotations

from decimal import Decimal

from grid.grid_state import GridState, save_state
from tracking.monitor import compute_funding_view
from tools import grid_monitor, run_grid


def test_funding_normalization_and_direction() -> None:
    """Variational 0.062%/8h vs Extended 0.000013(小数)/1h。"""
    v = compute_funding_view(Decimal("0.062"), Decimal("0.000013"))
    # Variational 已是 %/8h
    assert v.var_pct_8h == Decimal("0.062")
    # Extended: 0.000013 * 100 * 8 = 0.0104 %/8h
    assert abs(v.ext_pct_8h - Decimal("0.0104")) < Decimal("1e-9")
    # var > ext → 推荐 Variational 做空
    assert "做空" in v.recommended and "Variational" in v.recommended
    # 净 carry = 0.062 - 0.0104 = 0.0516 %/8h
    assert abs(v.carry_short_var_pct_8h - Decimal("0.0516")) < Decimal("1e-9")
    # 年化 = 0.0516 * 1095 ≈ 56.5%
    assert Decimal("55") < v.annualized_pct < Decimal("58")


def test_direction_flips_when_extended_higher() -> None:
    """Extended 资金费更高时应推荐反向。"""
    v = compute_funding_view(Decimal("0.001"), Decimal("0.0005"))
    # ext: 0.0005*100*8 = 0.4 %/8h > var 0.001 → 推荐 Variational 做多
    assert v.carry_short_var_pct_8h < 0
    assert "做多" in v.recommended and "Variational" in v.recommended
    assert v.annualized_pct > 0  # 推荐方向年化应为正


def test_run_grid_trend_aware_cli_defaults_and_overrides() -> None:
    """趋势感知 CLI 参数应有安全默认值，并完整透传给 GridConfig。"""
    parser = run_grid._build_parser()
    defaults = parser.parse_args([])
    assert defaults.trend_aware is False
    assert defaults.band_k == 1.75
    assert defaults.min_half_frac == 0.04
    assert defaults.hard_stop_dist == 0.12

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
