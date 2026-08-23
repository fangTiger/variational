"""Variational 执行模型与数量元数据测试。

数量约束的结构取自前端打包代码原文：
``qty_limits.bid`` / ``qty_limits.ask`` 各自带 min_qty / max_qty / min_qty_tick，
且前端按方向取值（买读 bid、卖读 ask）。这里的夹具与之保持一致。
"""

from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest

from adapters.base import ExchangeAdapter, Side
from adapters.variational_client import VariationalClient


def _client_with_quote(quote: object) -> tuple[VariationalClient, list[tuple]]:
    """构造只返回指定报价元数据的客户端，并记录真实询价调用。"""
    client = object.__new__(VariationalClient)
    client._quantity_limits = {}
    calls: list[tuple] = []

    async def request_quote(
        underlying: str,
        side: str,
        qty: Decimal,
        *,
        instrument_type: str = "perpetual_future",
        funding_interval_s: int = 3600,
        kind: str | None = None,
    ) -> object:
        calls.append((underlying, side, qty))
        return quote

    client.request_quote = request_quote
    return client, calls


#: 双侧对称的正常报价。
_SYMMETRIC = {
    "margin_requirements": {"initial_margin": "0.2"},
    "qty_limits": {
        "bid": {"min_qty": "0.00003", "max_qty": "5", "min_qty_tick": "0.00001"},
        "ask": {"min_qty": "0.00003", "max_qty": "5", "min_qty_tick": "0.00001"},
    },
}

#: 双侧不对称——用于证明按方向取值，而不是把两侧合并。
_ASYMMETRIC = {
    "qty_limits": {
        "bid": {"min_qty": "0.00002", "max_qty": "9", "min_qty_tick": "0.00001"},
        "ask": {"min_qty": "0.00007", "max_qty": "3", "min_qty_tick": "0.0001"},
    },
}


def test_execution_model_defaults_to_orderbook_and_variational_declares_rfq() -> None:
    """旧适配器默认走订单簿，Variational 明确声明 RFQ。"""
    assert ExchangeAdapter.execution_model == "orderbook"
    assert VariationalClient.execution_model == "rfq"


def test_get_min_order_size_uses_quote_quantity_limits() -> None:
    """最小量只能采用报价返回的明确数量限制。"""
    client, calls = _client_with_quote(_SYMMETRIC)

    minimum = asyncio.run(client.get_min_order_size("BTC"))

    assert minimum == Decimal("0.00003")
    assert calls == [("BTC", "buy", Decimal("0.0001"))]


def test_get_min_order_size_rejects_when_api_omits_quantity_limit() -> None:
    """API 未给最小量时必须失败关闭，不能编造零或默认值。

    下限缺失若被静默放过，会提交低于门槛的单并被交易所拒绝，
    而策略侧误判成「已下单」，最终留下单边裸仓。
    """
    client, _ = _client_with_quote({"margin_requirements": {"initial_margin": "0.2"}})

    with pytest.raises(RuntimeError, match="min_qty"):
        asyncio.run(client.get_min_order_size("BTC"))


def test_round_amount_aligns_down_using_quote_quantity_tick() -> None:
    """数量按步长向下对齐，避免放大目标仓位。"""
    client, _ = _client_with_quote(_SYMMETRIC)

    rounded = asyncio.run(client.round_amount("BTC", Decimal("0.000037")))

    assert rounded == Decimal("0.00003")


def test_round_amount_preserves_value_when_api_omits_quantity_tick() -> None:
    """步长未知时保持基类行为原样返回，不猜测精度。"""
    client, _ = _client_with_quote({"qty_limits": {"bid": {"min_qty": "0.00003"}}})

    rounded = asyncio.run(client.round_amount("BTC", Decimal("0.000037")))

    assert rounded == Decimal("0.000037")


# ---- 按方向取值：把 bid/ask 合并的实现必须跑不过下面这组 ----


def test_min_order_size_reads_bid_for_buy_and_ask_for_sell() -> None:
    """买单读 bid 侧、卖单读 ask 侧，与前端实现一致。

    若把两侧用 max() 合并，买卖都会得到 0.00007，买单侧就被抬到
    实际不需要的门槛，可能把本来合法的小额补单判成不可下单。
    """
    client, _ = _client_with_quote(_ASYMMETRIC)

    buy_min = asyncio.run(client.get_min_order_size("BTC", Side.BUY))
    sell_min = asyncio.run(client.get_min_order_size("BTC", Side.SELL))

    assert buy_min == Decimal("0.00002")
    assert sell_min == Decimal("0.00007")


def test_max_order_size_reads_matching_side() -> None:
    """单笔上限同样按方向取；这条决定大额单能不能下进去。"""
    client, _ = _client_with_quote(_ASYMMETRIC)

    assert asyncio.run(client.get_max_order_size("BTC", Side.BUY)) == Decimal("9")
    assert asyncio.run(client.get_max_order_size("BTC", Side.SELL)) == Decimal("3")


def test_round_amount_uses_side_specific_tick() -> None:
    """步长按方向取：两侧精度不同时不能串用。"""
    client, _ = _client_with_quote(_ASYMMETRIC)

    buy = asyncio.run(client.round_amount("BTC", Decimal("0.00012345"), Side.BUY))
    sell = asyncio.run(client.round_amount("BTC", Decimal("0.00012345"), Side.SELL))

    assert buy == Decimal("0.00012")
    assert sell == Decimal("0.0001")


def test_side_omitted_falls_back_to_strictest_limits() -> None:
    """不传方向时取更严格的组合：下限取大、步长取粗、上限取小。

    基类契约是单参数 (market)，timed_volume 现在仍按单参数调用，
    此时无法判断方向，只能保守——宁可少下也不要超限。
    """
    client, _ = _client_with_quote(_ASYMMETRIC)

    assert asyncio.run(client.get_min_order_size("BTC")) == Decimal("0.00007")
    assert asyncio.run(client.get_max_order_size("BTC")) == Decimal("3")
    assert asyncio.run(client.round_amount("BTC", Decimal("0.00012345"))) == Decimal(
        "0.0001"
    )


def test_max_order_size_returns_none_when_absent() -> None:
    """上限缺失只是没有约束，不能像下限那样抛异常。"""
    client, _ = _client_with_quote(
        {"qty_limits": {"bid": {"min_qty": "0.00003", "min_qty_tick": "0.00001"}}}
    )

    assert asyncio.run(client.get_max_order_size("BTC", Side.BUY)) is None


def test_quantity_limits_cached_per_market_and_side() -> None:
    """询价是真实 API 调用，同一 (market, side) 只能打一次。"""
    client, calls = _client_with_quote(_ASYMMETRIC)

    asyncio.run(client.get_min_order_size("BTC", Side.BUY))
    asyncio.run(client.get_max_order_size("BTC", Side.BUY))
    asyncio.run(client.round_amount("BTC", Decimal("1"), Side.BUY))

    assert len(calls) == 1

    asyncio.run(client.get_min_order_size("BTC", Side.SELL))

    assert len(calls) == 2
