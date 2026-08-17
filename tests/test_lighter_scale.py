"""Lighter 整数换算测试。

BTC 实测精度：size_decimals=5（BTC×100000）、price_decimals=1（USD×10）。
最小下单量 0.00020 BTC —— 低于它会被交易所拒单，进而触发引擎的 reject
cooldown 反复重试，所以必须在换算层就拦下。
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from adapters.lighter_scale import from_base_amount, to_base_amount, to_price


def test_btc_amount_scales_by_five_decimals():
    assert to_base_amount(Decimal("0.00317"), size_decimals=5) == 317


def test_btc_price_scales_by_one_decimal():
    assert to_price(Decimal("63400.0"), price_decimals=1) == 634000


def test_sub_precision_truncates_down():
    """向下取整。向上会让实际下单量超过引擎预期，可能突破库存上限。"""
    assert to_base_amount(Decimal("0.003179"), size_decimals=5) == 317


def test_integer_round_trip_is_lossless():
    """整数侧往返恒等。注意小数侧往返不恒等（1.5 在 decimals=0 下就回不来），
    所以这里断言的是整数往返，不是小数往返。"""
    assert to_base_amount(from_base_amount(317, 5), 5) == 317


def test_below_exchange_minimum_is_rejected():
    """0.0001 BTC 换算后是 10，非零但低于交易所最小量 0.00020（=20 单位）。
    不拦下来会被拒单，引擎进 reject cooldown 反复重试。"""
    with pytest.raises(ValueError, match="低于交易所最小下单量"):
        to_base_amount(Decimal("0.0001"), size_decimals=5, min_base_units=20)


def test_at_exchange_minimum_is_allowed():
    assert to_base_amount(Decimal("0.00020"), size_decimals=5, min_base_units=20) == 20


def test_zero_is_rejected():
    with pytest.raises(ValueError, match="必须为正"):
        to_base_amount(Decimal("0"), size_decimals=5)


def test_negative_is_rejected():
    """方向由 is_ask 表达，数量永远为正。传负数说明调用方搞错了。"""
    with pytest.raises(ValueError, match="必须为正"):
        to_base_amount(Decimal("-0.001"), size_decimals=5)


def test_float_is_rejected():
    """浮点会引入精度误差，强制传 Decimal。"""
    with pytest.raises(TypeError, match="Decimal"):
        to_base_amount(0.00317, size_decimals=5)


def test_amount_truncating_to_zero_is_rejected():
    with pytest.raises(ValueError, match="小于最小精度"):
        to_base_amount(Decimal("0.000001"), size_decimals=5)
