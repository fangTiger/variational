"""Lighter 订单视图层测试。

引擎全部通过属性读订单（.id / .status / .side / .filled_qty / .reduce_only /
.type / .price），而 Lighter 返回的是命名完全不同的 dict，且 side 字段实测为
空串。裸 dict 交给引擎会静默取到默认值——最危险的一条是 reduce_only 恒为
False，导致 filter_grid_orders 把交易所端保护单当成普通网格单撤掉。

因此这里的测试全部从"字段错位会造成什么后果"出发。
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from adapters.base import Side
from adapters.lighter_order import LighterOrder

# 实测样例（api.rh.lighter.xyz /api/v1/accountInactiveOrders）
REAL_ORDER = {
    "order_index": 844424907205585,
    "client_order_index": 826420772836,
    "order_id": "844424907205585",
    "market_index": 1,
    "owner_account_index": 5626,
    "initial_base_amount": "0.02936",
    "price": "64495.1",
    "remaining_base_amount": "0.00000",
    "is_ask": False,
    "filled_base_amount": "0.02936",
    "filled_quote_amount": "1893.58",
    "side": "",
    "type": "market",
    "time_in_force": "immediate-or-cancel",
    "reduce_only": False,
    "trigger_price": "0.0",
    "status": "filled",
    "trigger_status": "na",
    "created_at": 1787052005,
    "updated_at": 1787052005,
    "timestamp": 1787052005,
}


def test_side_derives_from_is_ask_not_from_side_field() -> None:
    """Lighter 的 side 字段实测为空串，方向只在 is_ask 里。

    直接透传 side 会让引擎拿到空方向，翻单逻辑判不出买卖，
    进而在错误的一侧重挂单。
    """
    buy = LighterOrder.from_api(dict(REAL_ORDER, is_ask=False))
    sell = LighterOrder.from_api(dict(REAL_ORDER, is_ask=True))

    assert buy.side == Side.BUY.value
    assert sell.side == Side.SELL.value


def test_reduce_only_is_preserved_so_protective_orders_survive() -> None:
    """reduce_only 必须如实透传。

    filter_grid_orders 用 getattr(o, "reduce_only", False) 挑"该撤的单"。
    若该属性缺失或恒为 False，交易所端的减仓保护单会被当成普通网格单撤掉。
    """
    protective = LighterOrder.from_api(dict(REAL_ORDER, reduce_only=True))
    ordinary = LighterOrder.from_api(dict(REAL_ORDER, reduce_only=False))

    assert protective.reduce_only is True
    assert ordinary.reduce_only is False


def test_id_is_client_order_index_not_exchange_order_index() -> None:
    """对引擎暴露的订单身份必须是 client_order_index。

    Lighter 下单响应只含 tx_hash，order_index 要事后查询才拿得到；
    而引擎在挂单返回值为空时会判定挂单失败并重挂（grid_engine.py:1770），
    留下挂在交易所却不被跟踪的孤儿单。client_order_index 是我们自己
    生成的，下单当场就有，因此选它做统一身份。

    引擎还会用存下的 id 去匹配历史订单（grid_engine.py:1009），
    所以这里的 id 必须与 place_limit_order 返回的是同一套编号。
    """
    order = LighterOrder.from_api(REAL_ORDER)

    assert order.id == 826420772836
    assert isinstance(order.id, int)


def test_order_index_kept_separately_for_cancellation() -> None:
    """撤单签名要的是整数 order_index，必须单独保留。"""
    order = LighterOrder.from_api(REAL_ORDER)

    assert order.order_index == 844424907205585
    assert isinstance(order.order_index, int)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("filled", "FILLED"),
        ("canceled", "CANCELLED"),
        ("cancelled", "CANCELLED"),
        ("expired", "EXPIRED"),
        ("open", "OPEN"),
    ],
)
def test_status_normalises_to_engine_vocabulary(raw: str, expected: str) -> None:
    """引擎按 FILLED/CANCELLED/EXPIRED/REJECTED 判终态。

    Lighter 返回小写，且用美式拼写 canceled（单 l）；
    不归一化会让终态判定整体失效，订单永远不被判成交，网格停止翻单。
    """
    order = LighterOrder.from_api(dict(REAL_ORDER, status=raw))

    assert order.status == expected


def test_filled_qty_comes_from_filled_base_amount_as_decimal() -> None:
    """引擎用 filled_qty > 0 区分"部分成交后被撤"与"纯撤单"。

    取错字段（比如拿 initial_base_amount）会把未成交的撤单误判为成交，
    在没有仓位的一侧翻单。
    """
    order = LighterOrder.from_api(
        dict(REAL_ORDER, initial_base_amount="0.05000", filled_base_amount="0.02000")
    )

    assert order.filled_qty == Decimal("0.02000")
    assert isinstance(order.filled_qty, Decimal)


def test_price_is_decimal_not_float() -> None:
    """价格必须是 Decimal，浮点会在对齐 tick 时引入误差。"""
    order = LighterOrder.from_api(REAL_ORDER)

    assert order.price == Decimal("64495.1")
    assert isinstance(order.price, Decimal)


def test_created_at_exposed_for_retry_reconciliation() -> None:
    """翻单重试对账要按下单时间窗过滤，缺时间字段会放行所有历史单。"""
    order = LighterOrder.from_api(REAL_ORDER)

    assert order.created_at == 1787052005


def test_type_normalised_for_grid_order_filter() -> None:
    """filter_grid_orders 用 type 排除 TPSL/CONDITIONAL，需大写比对。"""
    assert LighterOrder.from_api(dict(REAL_ORDER, type="limit")).type == "LIMIT"
    assert LighterOrder.from_api(dict(REAL_ORDER, type="market")).type == "MARKET"


def test_missing_required_field_raises_instead_of_defaulting() -> None:
    """缺字段必须炸，不能静默取默认值——那正是裸 dict 的失败模式。"""
    broken = dict(REAL_ORDER)
    del broken["order_index"]

    with pytest.raises(ValueError, match="order_index"):
        LighterOrder.from_api(broken)


def test_missing_client_order_index_raises() -> None:
    """client_order_index 是引擎侧身份，缺了会让订单匹配整体失效。

    若回落成 0，多笔订单会撞成同一个 id，引擎按 id 建的映射
    互相覆盖，撤单会撤错单。
    """
    broken = dict(REAL_ORDER)
    del broken["client_order_index"]

    with pytest.raises(ValueError, match="client_order_index"):
        LighterOrder.from_api(broken)
