"""K 线行情源。

从交易所适配器里独立出来，是为了让「在哪交易」和「用谁的 K 线」解耦：
Lighter 的 `/api/v1/candlesticks` 实测返回 403，做市引擎必须借 Extended 的
BTC K 线算 band。两所同一标的，实测基差约 0.08%，用于 ATR/ADX 完全够用。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol


@dataclass(frozen=True)
class Candle:
    """一根 K 线。只保留 band/regime 计算真正用到的字段。"""

    ts: int
    high: Decimal
    low: Decimal
    close: Decimal


class CandleSource(Protocol):
    """K 线来源。实现方决定从哪个交易所取。"""

    async def get_hourly_candles(self, market: str, limit: int) -> list[Candle]:
        ...


class ExtendedCandleSource:
    """从 Extended SDK 取小时 K 线。

    `market_override` 用于「在 A 所交易、用 B 所 K 线」的场景：同一标的在
    两所命名不同（Lighter 叫 `BTC`，Extended 叫 `BTC-USD`），不覆盖会
    直接报市场不存在。
    """

    def __init__(self, ext, market_override: str | None = None) -> None:
        self._ext = ext
        self._market_override = market_override

    async def get_hourly_candles(self, market: str, limit: int) -> list[Candle]:
        """取小时 K 线，按时间升序返回。

        失败一律抛出，绝不返回空列表：引擎对两者的处理不同——异常走
        「跳过补格」的降级分支并保留上一轮 band，空列表会被当成真实
        行情把 band 算成退化值。
        """
        response = await self._ext._client.info.get_candles_history(
            market_name=self._market_override or market,
            candle_type="trades",
            interval="PT1H",
            limit=limit,
        )
        rows = sorted(response.data or [], key=lambda k: int(k.timestamp))
        return [
            Candle(
                ts=int(k.timestamp),
                high=Decimal(str(k.high)),
                low=Decimal(str(k.low)),
                close=Decimal(str(k.close)),
            )
            for k in rows
        ]
