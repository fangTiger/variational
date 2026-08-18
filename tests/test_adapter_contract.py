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


@pytest.mark.parametrize("cls", [ExtendedClient, LighterClient])
def test_get_mark_price_takes_single_market_argument(cls):
    """引擎按位置传参调用 get_mark_price，参数个数分叉会在跨所时炸。

    只校验个数不校验名称：Extended 沿用 `market_name`、基类与 Lighter 用
    `market`，这是既有约定，按位置调用不受影响。
    """
    got = list(inspect.signature(cls.get_mark_price).parameters)
    assert len(got) == 2, f"{cls.__name__}.get_mark_price 参数个数不符：{got}"
    assert got[0] == "self"


def test_extended_mark_price_is_not_the_bid_ask_mid():
    """Extended 必须覆盖 get_mark_price，不能沿用基类的盘口中值。

    基类默认返回 get_market_price().mid，而 Extended 的 get_market_price
    取的是买一/卖一——盘口中值与标记价是两个口径。引擎的清算距离、硬止损
    和整仓 TPSL 都按标记价计算，用中值会让这些保护线整体偏移。
    """
    assert ExtendedClient.get_mark_price is not ExchangeAdapter.get_mark_price
