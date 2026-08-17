"""Lighter 下单参数的整数换算。

Lighter 的 create_order 接收整数：
    base_amount = BTC 数量 × 10**size_decimals
    price       = USD 价格 × 10**price_decimals

BTC 实测 size_decimals=5、price_decimals=1，即 0.00317 → 317、63400.0 → 634000。

换算错一位就是 10 倍仓位，因此一律用 Decimal、一律向下取整，
并对零值、负值、低于精度、低于交易所最小量的输入直接报错——
宁可下不出单，不能下错单。

取整方向统一向下截断，误差 ≤ 1 个精度单位（BTC 价格是 $0.1），
远小于网格格距（0.0986% ≈ $62），对策略无实质影响。
"""

from __future__ import annotations

from decimal import Decimal


def _check(value: Decimal, what: str) -> None:
    if not isinstance(value, Decimal):
        raise TypeError(
            f"{what}必须是 Decimal，收到 {type(value).__name__}；浮点会引入精度误差"
        )
    if value <= 0:
        raise ValueError(f"{what}必须为正，收到 {value}；方向由 is_ask 表达")


def to_base_amount(
    amount: Decimal, size_decimals: int, *, min_base_units: int | None = None
) -> int:
    """BTC 数量 → 整数。

    min_base_units 是交易所最小下单量的整数形式（BTC 为 20，即 0.00020）。
    传入后会拦下低于最小量的请求——否则交易所拒单会让引擎进 reject
    cooldown 反复重试。
    """
    _check(amount, "数量")
    scaled = int(amount * (10 ** size_decimals))
    if scaled == 0:
        raise ValueError(f"数量 {amount} 小于最小精度单位（10^-{size_decimals}）")
    if min_base_units is not None and scaled < min_base_units:
        raise ValueError(
            f"数量 {amount}（={scaled} 单位）低于交易所最小下单量 {min_base_units} 单位"
        )
    return scaled


def to_price(price: Decimal, price_decimals: int) -> int:
    """USD 价格 → 整数。"""
    _check(price, "价格")
    scaled = int(price * (10 ** price_decimals))
    if scaled == 0:
        raise ValueError(f"价格 {price} 小于最小精度单位（10^-{price_decimals}）")
    return scaled


def from_base_amount(scaled: int, size_decimals: int) -> Decimal:
    """整数 → BTC 数量。"""
    return Decimal(scaled) / (10 ** size_decimals)


def from_price(scaled: int, price_decimals: int) -> Decimal:
    """整数 → USD 价格。"""
    return Decimal(scaled) / (10 ** price_decimals)
