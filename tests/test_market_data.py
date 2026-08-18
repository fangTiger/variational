"""行情源测试。

K 线要能独立于交易所指定——这是 Lighter 做市场景能跑起来的前提：
Lighter 的 K 线接口返回 403，引擎必须用 Extended 的 BTC K 线算 band。
"""

from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest

from adapters.market_data import Candle, ExtendedCandleSource


class _FakeInfo:
    def __init__(self, rows):
        self._rows = rows
        self.calls = []

    async def get_candles_history(self, **kwargs):
        self.calls.append(kwargs)
        rows = self._rows

        class _R:
            data = rows

        return _R()


class _FakeExt:
    def __init__(self, rows):
        self.info = _FakeInfo(rows)
        self._client = type("C", (), {"info": self.info})()


def _row(ts, h, low, c):
    return type("K", (), {"timestamp": ts, "high": h, "low": low, "close": c})()


def test_candles_are_sorted_by_timestamp() -> None:
    """乱序 K 线会让 ATR/ADX 全错，必须排序。"""
    ext = _FakeExt([_row(3, 3, 1, 2), _row(1, 2, 1, 1), _row(2, 4, 2, 3)])

    out = asyncio.run(ExtendedCandleSource(ext).get_hourly_candles("BTC-USD", 3))

    assert [c.ts for c in out] == [1, 2, 3]


def test_candle_fields_are_decimal() -> None:
    """价格一律 Decimal，避免浮点误差混进 band 计算。"""
    ext = _FakeExt([_row(1, "2.5", "1.5", "2.0")])

    out = asyncio.run(ExtendedCandleSource(ext).get_hourly_candles("BTC-USD", 1))

    assert isinstance(out[0], Candle)
    assert out[0].high == Decimal("2.5")
    assert out[0].low == Decimal("1.5")
    assert out[0].close == Decimal("2.0")


def test_limit_and_market_are_passed_through() -> None:
    """limit 决定回看窗口；传错会让 band 宽度整体失真。"""
    ext = _FakeExt([_row(1, 1, 1, 1)])

    asyncio.run(ExtendedCandleSource(ext).get_hourly_candles("BTC-USD", 96))

    call = ext.info.calls[0]
    assert call["limit"] == 96
    assert call["market_name"] == "BTC-USD"
    assert call["interval"] == "PT1H"


def test_market_override_lets_lighter_borrow_extended_candles() -> None:
    """交易标的与 K 线标的可以不同名。

    Lighter 上的市场叫 'BTC'，Extended 上叫 'BTC-USD'；行情源必须
    按自己的命名去取，否则 Lighter 场景一取 K 线就报市场不存在。
    """
    ext = _FakeExt([_row(1, 1, 1, 1)])
    source = ExtendedCandleSource(ext, market_override="BTC-USD")

    asyncio.run(source.get_hourly_candles("BTC", 10))

    assert ext.info.calls[0]["market_name"] == "BTC-USD"


def test_empty_response_returns_empty_list_not_error() -> None:
    """没有 K 线时返回空列表，由调用方决定如何降级，不在这里抛。"""
    ext = _FakeExt([])

    assert asyncio.run(ExtendedCandleSource(ext).get_hourly_candles("BTC-USD", 5)) == []


def test_source_failure_propagates() -> None:
    """取 K 线失败必须抛出，不能返回空列表伪装成"没有行情"。

    引擎对空列表和异常的处理不同：异常会走"跳过补格"的降级分支并保留
    上一轮的 band，而空列表会被当成真实行情，把 band 算成退化值。
    """

    class _Boom:
        async def get_candles_history(self, **_kwargs):
            raise RuntimeError("行情接口故障")

    ext = _FakeExt([])
    ext._client = type("C", (), {"info": _Boom()})()

    with pytest.raises(RuntimeError, match="行情接口故障"):
        asyncio.run(ExtendedCandleSource(ext).get_hourly_candles("BTC-USD", 5))
