"""Lighter 订单的引擎视图。

`grid_engine` 全部通过属性读订单（`.id` / `.status` / `.side` / `.filled_qty` /
`.reduce_only` / `.type` / `.price` / `.created_at`），而 Lighter 的 REST 接口
返回命名完全不同的 dict。把裸 dict 交给引擎会静默取到默认值，其中最危险的是
`reduce_only` 恒为 False——`filter_grid_orders` 会因此把交易所端的减仓保护单
当成普通网格单一并撤掉。

本模块负责这层映射，并在关键字段缺失时直接抛错，而不是回落到默认值。

实测要点（api.rh.lighter.xyz）：
- `side` 字段返回**空串**，真实方向只在 `is_ask` 布尔里
- `status` 为小写，且撤单用美式拼写 `canceled`（单 l）
- 撤单要传整数 `order_index`，不是字符串 `order_id`
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from adapters.base import Side

# Lighter 小写状态 → 引擎终态词汇（FILLED/CANCELLED/EXPIRED/REJECTED）
_STATUS_ALIASES = {
    "CANCELED": "CANCELLED",
    "CANCELED-POST-ONLY": "CANCELLED-POST-ONLY",
    "CANCELED-SELF-TRADE": "CANCELLED-SELF-TRADE",
}


def _require(raw: dict[str, Any], key: str) -> Any:
    """取必需字段；缺失即抛错，避免静默降级成默认值。"""
    if key not in raw or raw[key] is None:
        raise ValueError(f"Lighter 订单缺少必需字段 {key}：{raw!r}")
    return raw[key]


@dataclass(frozen=True)
class LighterOrder:
    """引擎可直接读属性的订单视图。

    字段名对齐 `grid_engine` 的取值方式，不要改名。

    **`id` 是 `client_order_index`，不是交易所的 `order_index`。**
    Lighter 的下单响应只含 tx_hash，`order_index` 要事后查询才拿得到；
    而引擎在挂单返回值为空时会判定挂单失败并原格重挂
    （`grid_engine.py:1770`），留下挂在交易所却不被跟踪的孤儿单。
    `client_order_index` 由我们自己分配，下单当场即有，因此选作统一身份。
    引擎还会用存下的 id 匹配历史订单（`grid_engine.py:1009`），
    两处必须是同一套编号。

    交易所侧的 `order_index` 单独保留，只在撤单签名时使用。
    """

    id: int
    order_index: int
    status: str
    side: str
    price: Decimal
    qty: Decimal
    filled_qty: Decimal
    filled_quote_amount: Decimal
    reduce_only: bool
    type: str
    trigger_price: Decimal
    created_at: int
    raw: dict[str, Any]

    @property
    def average_price(self) -> Decimal | None:
        """按真实成交基础量与报价量计算均价；没有成交时不返回伪造零价。"""
        if self.filled_qty == 0 or self.filled_quote_amount == 0:
            return None
        return self.filled_quote_amount / self.filled_qty

    @property
    def is_position_stop_loss(self) -> bool:
        """是否为整仓止损单。

        按"reduce-only 且带触发价"判定，不依赖 `type` 字符串的具体拼写——
        Lighter 的类型词汇表未在文档中固定，用行为特征更稳。
        """
        return self.reduce_only and self.trigger_price > 0

    @classmethod
    def from_api_list(cls, raws: Any, *, context: str) -> list["LighterOrder"]:
        """批量构造；响应不是数组即抛错，避免把读取故障当成空单列表。"""
        if not isinstance(raws, list):
            raise ValueError(f"Lighter {context}响应缺少 orders 数组")
        return [cls.from_api(raw) for raw in raws]

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> "LighterOrder":
        """从 Lighter REST 响应构造；关键字段缺失直接抛错。"""
        if not isinstance(raw, dict):
            raise ValueError(f"Lighter 订单必须是 dict，收到 {type(raw).__name__}")

        order_index = int(_require(raw, "order_index"))
        # 引擎侧身份用 client_order_index，理由见类文档
        client_order_index = int(_require(raw, "client_order_index"))
        # side 字段实测为空串，方向只能从 is_ask 推导
        is_ask = bool(_require(raw, "is_ask"))
        status = str(_require(raw, "status")).upper()

        return cls(
            id=client_order_index,
            order_index=order_index,
            status=_STATUS_ALIASES.get(status, status),
            side=(Side.SELL if is_ask else Side.BUY).value,
            price=Decimal(str(_require(raw, "price"))),
            qty=Decimal(str(raw.get("initial_base_amount") or 0)),
            filled_qty=Decimal(str(raw.get("filled_base_amount") or 0)),
            filled_quote_amount=Decimal(str(raw.get("filled_quote_amount") or 0)),
            reduce_only=bool(raw.get("reduce_only", False)),
            type=str(raw.get("type") or "").upper(),
            trigger_price=Decimal(str(raw.get("trigger_price") or 0)),
            created_at=int(raw.get("created_at") or raw.get("timestamp") or 0),
            raw=raw,
        )


def filter_grid_orders(open_orders: list[LighterOrder]) -> list[LighterOrder]:
    """挑出"普通网格单"（该撤的），保留 reduce-only 与条件单。

    与 Extended 侧同名函数语义一致：撤错会让交易所端的整仓止损消失，
    网格在急跌里失去最后一道保护。
    """
    return [
        order
        for order in open_orders
        if not order.reduce_only and order.type not in ("TPSL", "CONDITIONAL")
    ]
