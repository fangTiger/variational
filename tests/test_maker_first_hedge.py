"""maker 优先对冲测试。

核心保证：无论走哪条路径，敞口最终都被精确覆盖——
既不能漏（欠对冲），也不能超（超额对冲）。
"""

from __future__ import annotations

import asyncio
import inspect
from decimal import Decimal

import pytest

from adapters.base import MarketPrice, Position, Side
from adapters.extended_client import ExtendedClient
from engine.hedge_engine import (
    HedgeConfig,
    HedgeEngine,
    HedgeFillResult,
    maker_first_hedge,
)


class _Order:
    """假订单。模拟 ExtendedClient.get_order_by_id 的返回结构。"""

    def __init__(self, order_id, filled=Decimal("0"), status="NEW"):
        self.id = order_id
        self.filled_qty = filled
        self.status = status


class _Response:
    """模拟 Extended 下单接口的包装响应，订单号位于 data.id。"""

    def __init__(self, data):
        self.data = data


class _Adapter:
    """假适配器。所有交易方法的签名与 ExtendedClient 一致。"""

    def __init__(
        self,
        fill_sequence=None,
        cancel_error=None,
        size=Decimal("0"),
    ):
        """fill_sequence: 每次查询订单返回的已成交量或 (成交量, 状态)。"""
        self.limit_orders = []
        self.market_orders = []
        self.cancelled = []
        self.hedge_calls = []
        self.order_reads = 0
        self._fills = list(fill_sequence or [Decimal("0")])
        self._cancel_error = cancel_error
        self._size = size

    async def get_market_price(self, market_name):
        return MarketPrice(
            market=market_name,
            bid=Decimal("60000"),
            ask=Decimal("60001"),
        )

    async def get_position(self, market):
        return Position(market, self._size)

    async def place_limit_order(
        self,
        market,
        side,
        amount,
        price,
        *,
        post_only=True,
        expire_days=90,
        reduce_only=False,
    ):
        self.limit_orders.append(
            {
                "market": market,
                "side": side,
                "amount": amount,
                "price": price,
                "post_only": post_only,
                "expire_days": expire_days,
                "reduce_only": reduce_only,
            }
        )
        return _Response(_Order("L1"))

    async def get_order_by_id(self, market, order_id):
        self.order_reads += 1
        value = self._fills[0] if len(self._fills) == 1 else self._fills.pop(0)
        if isinstance(value, tuple):
            filled, status = value
        else:
            filled = value
            status = "FILLED" if filled >= self.limit_orders[0]["amount"] else "NEW"
        return _Order(order_id, filled=filled, status=status)

    async def cancel_order(self, market, order_id):
        if self._cancel_error:
            raise RuntimeError(self._cancel_error)
        self.cancelled.append(order_id)

    async def market_order(self, market, side, amount, *, reduce_only=False):
        self.market_orders.append(
            {
                "market": market,
                "side": side,
                "amount": amount,
                "reduce_only": reduce_only,
            }
        )
        return _Response(_Order("M1", filled=amount, status="FILLED"))

    async def hedge(self, market, target_signed_size):
        self.hedge_calls.append((market, target_signed_size))
        return _Response(_Order("M1", status="FILLED"))


class _AdapterWithoutOrderLookup:
    """刻意不实现单查能力，模拟尚未补齐能力的新交易适配器。"""

    def __init__(self, position_sequence) -> None:
        self._positions = list(position_sequence)
        self.cancelled: list[str] = []
        self.market_orders: list[dict] = []

    async def get_market_price(self, market):
        return MarketPrice(market, bid=Decimal("60000"), ask=Decimal("60001"))

    async def get_position(self, market):
        size = self._positions[0] if len(self._positions) == 1 else self._positions.pop(0)
        return Position(market, Decimal(str(size)))

    async def place_limit_order(
        self,
        market,
        side,
        amount,
        price,
        *,
        post_only=True,
        reduce_only=False,
    ):
        del market, side, amount, price, post_only, reduce_only
        return _Response(_Order("L1"))

    async def cancel_order(self, market, order_id):
        del market
        self.cancelled.append(order_id)

    async def market_order(self, market, side, amount, *, reduce_only=False):
        self.market_orders.append(
            {
                "market": market,
                "side": side,
                "amount": amount,
                "reduce_only": reduce_only,
            }
        )
        return _Response(_Order("M1", filled=amount, status="FILLED"))


def _signature_shape(method):
    """只比较调用契约，不要求测试桩复制生产类型注解。"""
    return [
        (parameter.name, parameter.kind, parameter.default)
        for parameter in inspect.signature(method).parameters.values()
    ]


@pytest.mark.parametrize(
    "method_name",
    [
        "get_market_price",
        "place_limit_order",
        "get_order_by_id",
        "cancel_order",
        "market_order",
    ],
)
def test_fake_adapter_signature_matches_extended_client(method_name):
    """测试桩必须锁定真实接口，避免测试全绿但生产因签名不符崩溃。"""
    assert _signature_shape(getattr(_Adapter, method_name)) == _signature_shape(
        getattr(ExtendedClient, method_name)
    )


def _run(
    adapter,
    delta=Decimal("-1"),
    *,
    timeout_s=0.005,
    poll_s=0.01,
    **kwargs,
):
    return asyncio.run(
        maker_first_hedge(
            adapter,
            "BTC-USD",
            delta,
            timeout_s=timeout_s,
            poll_s=poll_s,
            **kwargs,
        )
    )


def test_sell_places_maker_at_ask():
    """卖出挂在 best ask。挂在 bid 会立刻成交，被 post_only 拒绝。"""
    adapter = _Adapter(fill_sequence=[Decimal("1")])

    _run(adapter, delta=Decimal("-1"))

    assert adapter.limit_orders[0]["price"] == Decimal("60001")
    assert adapter.limit_orders[0]["side"] is Side.SELL
    assert adapter.limit_orders[0]["post_only"] is True


def test_buy_places_maker_at_bid():
    adapter = _Adapter(fill_sequence=[Decimal("1")])

    _run(adapter, delta=Decimal("1"))

    assert adapter.limit_orders[0]["price"] == Decimal("60000")
    assert adapter.limit_orders[0]["side"] is Side.BUY


def test_full_maker_fill_avoids_taker():
    """maker 全部成交后不得再吃单——那会变成双倍仓位。"""
    adapter = _Adapter(fill_sequence=[Decimal("1")])

    result = _run(adapter)

    assert adapter.market_orders == []
    assert result.used_taker is False
    assert result.filled == Decimal("1")


def test_timeout_cancels_then_takes_full_amount():
    adapter = _Adapter(fill_sequence=[Decimal("0")])

    result = _run(adapter)

    assert adapter.cancelled == ["L1"]
    assert len(adapter.market_orders) == 1
    assert adapter.market_orders[0]["amount"] == Decimal("1")
    assert result.used_taker is True


def test_partial_fill_takes_only_remainder():
    """部分成交后只吃差额，按原始量吃会超额对冲。"""
    adapter = _Adapter(fill_sequence=[Decimal("0.4")])

    result = _run(adapter)

    assert len(adapter.market_orders) == 1
    assert adapter.market_orders[0]["amount"] == Decimal("0.6")
    assert result.filled == Decimal("1")


def test_successful_cancel_rereads_before_taking():
    """撤单成功也要重读，覆盖撤单前瞬间从 40% 追加成交到 70% 的竞态。"""
    adapter = _Adapter(fill_sequence=[Decimal("0.4"), Decimal("0.7")])

    _run(adapter)

    assert adapter.cancelled == ["L1"]
    assert adapter.order_reads == 2
    assert adapter.market_orders[0]["amount"] == Decimal("0.3")


def test_cancel_failure_rereads_before_taking():
    """撤单失败后按重读成交量决定吃多少，不能无条件吃全量。"""
    adapter = _Adapter(
        fill_sequence=[Decimal("0.4"), Decimal("0.7")],
        cancel_error="撤单失败",
    )

    result = _run(adapter)

    assert adapter.order_reads == 2
    assert adapter.market_orders[0]["amount"] == Decimal("0.3")
    assert "撤单失败" in result.note


def test_fully_filled_after_cancel_failure_places_no_taker():
    """轮询时未成交，撤单失败后重读已全成，绝不能再吃单。"""
    adapter = _Adapter(
        fill_sequence=[Decimal("0"), Decimal("1")],
        cancel_error="撤单失败",
    )

    result = _run(adapter)

    assert adapter.order_reads == 2
    assert adapter.market_orders == []
    assert result.used_taker is False


def test_missing_order_lookup_capability_uses_safe_fallback() -> None:
    """真实缺少单查方法时不得泄漏 AttributeError / NotImplementedError。"""
    adapter = _AdapterWithoutOrderLookup([Decimal("0"), Decimal("0")])
    assert not hasattr(adapter, "get_order_by_id")

    result = _run(adapter, timeout_s=0.0)

    assert adapter.cancelled == ["L1"]
    assert adapter.market_orders[0]["amount"] == Decimal("1")
    assert result.used_taker is True


def test_missing_order_lookup_fallback_uses_actual_position_delta() -> None:
    """maker 已成交 0.4 时只补 0.6，绝不能按原下单量再吃 1.0。"""
    adapter = _AdapterWithoutOrderLookup(
        [Decimal("0"), Decimal("-0.4")]
    )
    assert not hasattr(adapter, "get_order_by_id")

    result = _run(adapter, timeout_s=0.0)

    assert adapter.cancelled == ["L1"]
    assert adapter.market_orders == [
        {
            "market": "BTC-USD",
            "side": Side.SELL,
            "amount": Decimal("0.6"),
            "reduce_only": False,
        }
    ]
    assert result.filled == Decimal("1")


def test_zero_delta_places_nothing():
    adapter = _Adapter()

    result = _run(adapter, delta=Decimal("0"))

    assert adapter.limit_orders == [] and adapter.market_orders == []
    assert result.filled == Decimal("0")


def test_engine_uses_taker_by_default():
    """默认保持现状，继续委托既有 hedge() 直接吃单。"""
    adapter = _Adapter()
    config = HedgeConfig(dry_run=False)
    engine = HedgeEngine(adapter, adapter, config)

    result = asyncio.run(engine._rebalance(Decimal("-1")))

    assert config.maker_first_timeout_s == 0.0
    assert adapter.hedge_calls == [("BTC-USD", Decimal("-1"))]
    assert adapter.limit_orders == []
    assert result == "再平衡 hedge → -1"


def test_engine_maker_first_enabled_when_timeout_positive(monkeypatch):
    """正数超时才启用 maker，并按当前仓位计算变化量。"""
    calls = []

    async def fake_maker_first(
        adapter,
        market,
        target_delta,
        *,
        timeout_s,
        poll_s=1.0,
        reduce_only=False,
    ):
        calls.append(
            {
                "adapter": adapter,
                "market": market,
                "target_delta": target_delta,
                "timeout_s": timeout_s,
                "reduce_only": reduce_only,
            }
        )
        return HedgeFillResult(
            filled=abs(target_delta),
            used_taker=False,
            note="maker 全部成交",
        )

    monkeypatch.setattr("engine.hedge_engine.maker_first_hedge", fake_maker_first)
    adapter = _Adapter(size=Decimal("-0.4"))
    config = HedgeConfig(dry_run=False, maker_first_timeout_s=15.0)
    engine = HedgeEngine(adapter, adapter, config)

    result = asyncio.run(engine._rebalance(Decimal("-1")))

    assert calls == [
        {
            "adapter": adapter,
            "market": "BTC-USD",
            "target_delta": Decimal("-0.6"),
            "timeout_s": 15.0,
            "reduce_only": False,
        }
    ]
    assert adapter.hedge_calls == []
    assert result == "再平衡 hedge → -1（maker 全部成交）"


def test_engine_maker_first_preserves_reduce_only(monkeypatch):
    """同方向缩仓必须延续既有 hedge() 的 reduce_only 语义。"""
    captured = {}

    async def fake_maker_first(
        adapter,
        market,
        target_delta,
        *,
        timeout_s,
        poll_s=1.0,
        reduce_only=False,
    ):
        captured.update(
            target_delta=target_delta,
            reduce_only=reduce_only,
        )
        return HedgeFillResult(
            filled=abs(target_delta),
            used_taker=False,
            note="maker 全部成交",
        )

    monkeypatch.setattr("engine.hedge_engine.maker_first_hedge", fake_maker_first)
    adapter = _Adapter(size=Decimal("-1"))
    config = HedgeConfig(dry_run=False, maker_first_timeout_s=15.0)
    engine = HedgeEngine(adapter, adapter, config)

    asyncio.run(engine._rebalance(Decimal("-0.4")))

    assert captured == {
        "target_delta": Decimal("0.6"),
        "reduce_only": True,
    }
