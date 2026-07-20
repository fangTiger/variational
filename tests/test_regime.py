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


def test_killswitch_off_in_strong_trend() -> None:
    highs, lows, closes = _synthetic()
    a = adx(highs, lows, closes)
    up, lo = donchian_prev(highs, lows, 20)
    # 趋势段应急停
    assert decide_mode(adx_val=a[95], close=closes[95], donchian_up=up[95], donchian_lo=lo[95]) is GridMode.OFF
    # 震荡段应中性
    assert decide_mode(adx_val=a[50], close=closes[50], donchian_up=up[50], donchian_lo=lo[50]) is GridMode.NEUTRAL


def test_warmup_defaults_off() -> None:
    # 指标预热不足(None) → 保守 OFF
    assert decide_mode(adx_val=None, close=100, donchian_up=None, donchian_lo=None) is GridMode.OFF


def test_breakout_triggers_off() -> None:
    # ADX 低但价格突破通道上沿 → OFF
    assert decide_mode(adx_val=15, close=110, donchian_up=105, donchian_lo=95) is GridMode.OFF


def test_adx_hysteresis() -> None:
    """ADX 迟滞：>30 触发 OFF，回落到 <27 才恢复，27-30 之间保持原状态。"""
    kw = dict(close=100, donchian_up=105, donchian_lo=95)
    # 中性时 28 不触发（阈值 30）
    assert decide_mode(adx_val=28, prev_mode=GridMode.NEUTRAL, **kw) is GridMode.NEUTRAL
    # 中性时 31 触发 OFF
    assert decide_mode(adx_val=31, prev_mode=GridMode.NEUTRAL, **kw) is GridMode.OFF
    # 已 OFF 时 28 仍保持 OFF（须 <27 才恢复）
    assert decide_mode(adx_val=28, prev_mode=GridMode.OFF, **kw) is GridMode.OFF
    # 已 OFF 时 26 恢复中性
    assert decide_mode(adx_val=26, prev_mode=GridMode.OFF, **kw) is GridMode.NEUTRAL
    # 不传 prev_mode（旧调用方式）行为不变：阈值 30
    assert decide_mode(adx_val=28, **kw) is GridMode.NEUTRAL


if __name__ == "__main__":
    test_adx_low_in_range_high_in_trend()
    test_ema_and_donchian_produce_values()
    test_killswitch_off_in_strong_trend()
    test_warmup_defaults_off()
    test_breakout_triggers_off()
    test_adx_hysteresis()
    print("✅ regime 测试通过")
