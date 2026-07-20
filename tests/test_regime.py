"""网格 regime 判断测试：指标合理性 + 急停决策。"""

from __future__ import annotations

import math

from grid.regime import GridMode, adx, decide_mode, donchian_prev, ema


def _synthetic():
    """前段震荡、后段强上涨的合成序列。"""
    closes = [100 + math.sin(i / 3) * 2 for i in range(60)] + [100 + i * 2 for i in range(40)]
    highs = [c + 1 for c in closes]
    lows = [c - 1 for c in closes]
    return highs, lows, closes


def test_adx_low_in_range_high_in_trend() -> None:
    highs, lows, closes = _synthetic()
    a = adx(highs, lows, closes)
    assert a[50] is not None and a[50] < 25       # 震荡段低
    assert a[95] is not None and a[95] > 50       # 趋势段高


def test_ema_and_donchian_produce_values() -> None:
    highs, lows, closes = _synthetic()
    assert ema(closes, 10)[-1] is not None
    up, lo = donchian_prev(highs, lows, 20)
    assert up[-1] is not None and lo[-1] is not None


def test_default_keeps_quoting_even_in_trend_and_breakout() -> None:
    """新策略（2026-07-21）：默认不再因通道突破/强趋势急停——网格持续挂单。"""
    highs, lows, closes = _synthetic()
    a = adx(highs, lows, closes)
    up, lo = donchian_prev(highs, lows, 20)
    # 强趋势段：默认仍中性（不再 ADX 急停）
    assert decide_mode(adx_val=a[95], close=closes[95], donchian_up=up[95], donchian_lo=lo[95]) is GridMode.NEUTRAL
    # 价格突破通道上沿：默认不再急停
    assert decide_mode(adx_val=15, close=110, donchian_up=105, donchian_lo=95) is GridMode.NEUTRAL
    # 情绪极值：默认不再急停
    assert decide_mode(adx_val=24, close=100, donchian_up=105, donchian_lo=95, fng=90) is GridMode.NEUTRAL


def test_warmup_defaults_off() -> None:
    # 指标预热不足(None) → 保守 OFF（仅冷启动瞬时）
    assert decide_mode(adx_val=None, close=100, donchian_up=None, donchian_lo=None) is GridMode.OFF


def test_adx_brake_is_opt_in() -> None:
    """ADX 熔断改为可选：默认禁用（阈值 999），显式调低才生效，迟滞照旧。"""
    # 默认极高阈值 → 即便 ADX=60 也不停
    assert decide_mode(adx_val=60, close=100, donchian_up=105, donchian_lo=95) is GridMode.NEUTRAL
    # 显式开启熔断：中性时 31>30 触发 OFF
    assert decide_mode(adx_val=31, adx_off=30, adx_resume=27, prev_mode=GridMode.NEUTRAL) is GridMode.OFF
    # 已 OFF 时 28 仍 OFF（须 <27 才恢复）
    assert decide_mode(adx_val=28, adx_off=30, adx_resume=27, prev_mode=GridMode.OFF) is GridMode.OFF
    # 已 OFF 时 26 恢复中性
    assert decide_mode(adx_val=26, adx_off=30, adx_resume=27, prev_mode=GridMode.OFF) is GridMode.NEUTRAL


if __name__ == "__main__":
    test_adx_low_in_range_high_in_trend()
    test_ema_and_donchian_produce_values()
    test_default_keeps_quoting_even_in_trend_and_breakout()
    test_warmup_defaults_off()
    test_adx_brake_is_opt_in()
    print("✅ regime 测试通过")
