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


@dataclass(frozen=True)
class PositionPnl:
    """单个市场的盈亏快照；字段取不到时为 None。"""

    unrealized_pnl: Decimal | None
    entry_price: Decimal | None
    position_value: Decimal | None


class ExchangeAdapter(ABC):
    """交易所适配器抽象基类（异步）。

    子类必须实现只读查询与 `market_order`；`hedge` 与 `close_position`
    在基类用通用逻辑实现，子类通常无需重写。

    `execution_model` 声明执行模型：`"orderbook"` 表示支持订单簿限价单，
    `"rfq"` 表示只能先询价再接受报价成交。未声明的旧适配器默认按订单簿处理。
    """

    #: 适配器名称，用于日志（子类覆盖）
    name: str = "exchange"
    #: 执行模型；订单簿适配器用 orderbook，纯询价适配器用 rfq。
    execution_model: str = "orderbook"
    #: 是否允许引擎自动改变仓位；人工只读腿必须覆盖为 False。
    supports_trading: bool = True

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

    async def cancel_order(self, market: str, order_id) -> None:
        """撤掉指定订单。

        market 参数对 Extended 是冗余的（订单号全局唯一），但 Lighter 撤单
        必须同时提供 market_index，因此契约统一带上它。
        """
        raise NotImplementedError

    @abstractmethod
    async def close(self) -> None:
        """释放连接资源。"""

    async def get_free_margin_ratio(self) -> Decimal | None:
        """可用保证金 / 权益（0~1）。用于自动降险；无法获取返回 None。"""
        return None

    async def get_position_pnl(self, market: str) -> PositionPnl | None:
        """返回单市场盈亏快照；不支持该能力时返回 None。"""
        del market
        return None

    async def get_liquidation_info(self, market: str) -> tuple[Decimal, Decimal] | None:
        """返回 (mark_price, liquidation_price)。无持仓/无法获取返回 None。

        用于"逼近清仓价即两腿一起平"的保护。
        """
        return None

    async def cancel_grid_orders(self, market: str) -> int:
        """只撤普通网格单，保留交易所端保护单。"""
        raise NotImplementedError

    async def round_price(self, market: str, price: Decimal) -> Decimal:
        """按市场价格 tick 对齐；不支持市场元数据的适配器原样返回。

        这是可选能力，保留默认实现以兼容旧适配器与测试桩。
        """
        del market
        return Decimal(str(price))

    async def round_amount(self, market: str, amount: Decimal) -> Decimal:
        """按市场数量步长对齐；不支持市场元数据的适配器原样返回。

        这是可选能力，保留默认实现以兼容旧适配器与测试桩。
        """
        del market
        return Decimal(str(amount))

    async def get_price_tick_size(self, market: str) -> Decimal | None:
        """返回市场价格 tick；无法取得时返回 None。"""
        del market
        return None

    async def get_min_order_size(self, market: str) -> Decimal:
        """返回市场最小下单量；适配器无法提供时应抛出异常。"""
        del market
        raise NotImplementedError

    async def get_mark_price(self, market: str) -> Decimal:
        """标记价。默认取买卖中值；有独立标记价的交易所应覆盖。"""
        return (await self.get_market_price(market)).mid

    async def place_position_stop_loss(
        self,
        market: str,
        signed_size: Decimal,
        trigger_price: Decimal,
    ):
        """为当前整仓挂交易所端止损。"""
        raise NotImplementedError

    async def cancel_tpsl(self, market: str) -> None:
        """撤掉当前市场的整仓 TPSL。"""
        raise NotImplementedError

    async def get_position_tpsl(self, market: str) -> object | None:
        """查询当前市场真实挂出的整仓 TPSL。"""
        raise NotImplementedError

    async def get_orders_history(
        self,
        market: str,
        limit: int = 100,
        *,
        order_type: str | None = None,
        sort: str | None = None,
    ) -> list:
        """查询历史订单，可在服务端按类型过滤和更新时间排序。"""
        raise NotImplementedError

    async def get_order_by_id(self, market: str, order_id) -> object:
        """按交易所订单 ID 查询单笔订单。"""
        raise NotImplementedError

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
