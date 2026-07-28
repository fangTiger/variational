"""二态趋势门控测试：震荡=NEUTRAL、强趋势=OFF、迟滞、只用已收盘K线。"""
from __future__ import annotations

import math

from grid.regime import GridMode, drop_forming_candle, trend_gate


def _synthetic():
    closes = [100 + math.sin(i / 3) * 2 for i in range(60)] + [100 + i * 2 for i in range(40)]
    highs = [c + 1 for c in closes]
    lows = [c - 1 for c in closes]
    return highs, lows, closes


def test_range_is_neutral() -> None:
    highs, lows, closes = _synthetic()
    m = trend_gate(highs[:55], lows[:55], closes[:55], adx_off=30, adx_resume=25,
                   prev_mode=GridMode.NEUTRAL)
    assert m is GridMode.NEUTRAL


def test_strong_trend_is_off() -> None:
    highs, lows, closes = _synthetic()
    m = trend_gate(highs, lows, closes, adx_off=30, adx_resume=25, prev_mode=GridMode.NEUTRAL)
    assert m is GridMode.OFF


def test_hysteresis_stays_off_until_resume() -> None:
    # 已 OFF：ADX 在 resume 与 off 之间应保持 OFF（迟滞）
    highs, lows, closes = _synthetic()
    # 截到第 66 根时末端 ADX≈28.39，位于 resume=25 与 off=30 之间
    highs, lows, closes = highs[:66], lows[:66], closes[:66]
    neutral = trend_gate(highs, lows, closes, adx_off=30, adx_resume=25,
                         prev_mode=GridMode.NEUTRAL)
    off = trend_gate(highs, lows, closes, adx_off=30, adx_resume=25,
                     prev_mode=GridMode.OFF)
    assert neutral is GridMode.NEUTRAL
    assert off is GridMode.OFF


def test_drop_forming_candle() -> None:
    # 最后一根未收盘 → 去掉后长度 -1，且末元素变为倒数第二根
    xs = [1.0, 2.0, 3.0]
    assert drop_forming_candle(xs) == [1.0, 2.0]
