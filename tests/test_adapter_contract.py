"""适配器契约测试。

存在理由：Extended 的 cancel_order 是一个参数，而 Lighter 撤单需要
market + order_index 两个信息。若两边签名不一致，跨适配器的代码会踩
TypeError；更糟的是假对象若照抄错误签名，测试会全绿而生产必炸。
本文件用 inspect.signature 把契约钉死。
"""

from __future__ import annotations

import inspect

import pytest

from adapters.base import ExchangeAdapter
from adapters.extended_client import ExtendedClient
from adapters.lighter_client import LighterClient
from adapters.variational_client import VariationalClient

_CANCEL_ADAPTERS = [ExtendedClient, LighterClient, VariationalClient]
_MARKET_ORDER_ADAPTERS = [ExtendedClient, LighterClient]


@pytest.mark.parametrize("cls", _CANCEL_ADAPTERS)
def test_cancel_order_takes_market_and_order_id(cls):
    params = list(inspect.signature(cls.cancel_order).parameters)
    assert params[:3] == ["self", "market", "order_id"], (
        f"{cls.__name__}.cancel_order 签名不符合契约，实际 {params}"
    )


@pytest.mark.parametrize("cls", _MARKET_ORDER_ADAPTERS)
def test_market_order_signature_matches_base(cls):
    base = list(inspect.signature(ExchangeAdapter.market_order).parameters)
    got = list(inspect.signature(cls.market_order).parameters)
    assert got == base, f"{cls.__name__}.market_order 与基类不一致：{got} != {base}"


def test_base_declares_cancel_order():
    """契约要写在基类上，否则各适配器各写各的，迟早再次分叉。"""
    assert hasattr(ExchangeAdapter, "cancel_order")
    params = list(inspect.signature(ExchangeAdapter.cancel_order).parameters)
    assert params[:3] == ["self", "market", "order_id"]
