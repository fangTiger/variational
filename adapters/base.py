"""交易所适配器统一接口与共享数据类型。

对冲引擎只依赖本模块定义的抽象接口 `ExchangeAdapter`，不感知具体是
Variational 还是 Extended。各适配器把自身的字段/枚举映射到这里的统一类型。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum


class Side(Enum):
    """下单方向。"""

    BUY = "BUY"
    SELL = "SELL"

    def opposite(self) -> "Side":
        return Side.SELL if self is Side.BUY else Side.BUY


@dataclass(frozen=True)
class MarketPrice:
    """某标的的买一/卖一价。"""

    market: str
    bid: Decimal
    ask: Decimal

    @property
    def mid(self) -> Decimal:
        return (self.bid + self.ask) / 2


@dataclass(frozen=True)
class Position:
    """持仓快照。signed_size 已归一化：>0 多头，<0 空头，0 无仓位。"""

    market: str
    signed_size: Decimal
    raw: object = None

    @property
    def is_flat(self) -> bool:
        return self.signed_size == 0


class ExchangeAdapter(ABC):
    """交易所适配器抽象基类（异步）。

    子类必须实现只读查询与 `market_order`；`hedge` 与 `close_position`
    在基类用通用逻辑实现，子类通常无需重写。
    """

    #: 适配器名称，用于日志（子类覆盖）
    name: str = "exchange"

    @abstractmethod
    async def connect(self) -> None:
        """建立连接、加载必要的元数据。"""

    @abstractmethod
    async def get_market_price(self, market: str) -> MarketPrice:
        """获取买一/卖一价。"""

    @abstractmethod
    async def get_position(self, market: str) -> Position:
        """获取某标的当前持仓。"""

    @abstractmethod
    async def market_order(
        self, market: str, side: Side, amount: Decimal, *, reduce_only: bool = False
    ):
        """以市价单开/平仓，返回平台原始下单结果。"""

    @abstractmethod
    async def close(self) -> None:
        """释放连接资源。"""

    async def get_free_margin_ratio(self) -> Decimal | None:
        """可用保证金 / 权益（0~1）。用于自动降险；无法获取返回 None。"""
        return None

    # ---- 通用逻辑（子类一般无需重写）----

    async def hedge(self, market: str, target_signed_size: Decimal):
        """把某标的持仓调整到目标有符号数量。

        target_signed_size > 0 目标净多，< 0 目标净空。返回下单结果或 None（无需调整）。
        """
        current = await self.get_position(market)
        delta = target_signed_size - current.signed_size
        if delta == 0:
            return None
        side = Side.BUY if delta > 0 else Side.SELL
        # 仅当是"缩小同方向仓位"时才用 reduce_only，避免误开反向
        reduce_only = (
            abs(target_signed_size) < abs(current.signed_size)
            and target_signed_size * current.signed_size >= 0
        )
        return await self.market_order(market, side, abs(delta), reduce_only=reduce_only)

    async def close_position(self, market: str):
        """市价平掉某标的全部仓位。"""
        pos = await self.get_position(market)
        if pos.is_flat:
            return None
        side = Side.SELL if pos.signed_size > 0 else Side.BUY
        return await self.market_order(market, side, abs(pos.signed_size), reduce_only=True)
