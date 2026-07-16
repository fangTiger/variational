"""资金费归一化与方向选择测试。"""

from __future__ import annotations

from decimal import Decimal

from tracking.monitor import compute_funding_view


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


if __name__ == "__main__":
    test_funding_normalization_and_direction()
    test_direction_flips_when_extended_higher()
    print("✅ monitor 测试通过")
