"""Extended（x10 / Starknet）交易所适配器——对冲腿。

对冲策略里 Extended 承担与 Variational 反向的等额仓位，把方向性风险对冲掉。
本适配器封装官方 SDK `x10-python-trading-starknet`（v2.x，Starknet 分支），
只暴露对冲引擎需要的最小接口：查行情/持仓/余额、市价开仓、平仓、设杠杆。

依赖与凭证（.env，绝不入库）：
    pip install x10-python-trading-starknet
    X10_CLIENT_CONFIG_NAME=MAINNET       # 或 TESTNET
    X10_API_KEY=...
    X10_PUBLIC_KEY=0x...                  # Stark 公钥
    X10_PRIVATE_KEY=0x...                 # Stark 私钥
    X10_VAULT_ID=...                      # 账户 vault id
凭证从 Extended 的 api-management 页面生成。

⚠️ 阶段二状态：开/平仓/查询流程按官方示例实现，但持仓/余额模型的**字段名**
（size/side/balance 等）需在有真实账户后跑一次确认（见方法内 TODO）。
实盘前务必用极小仓位在 TESTNET 验证。
"""

from __future__ import annotations

import os
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import ROUND_DOWN, Decimal

from x10.clients.rest.rest_api_client import RestApiClient
from x10.config import get_config_by_name
from x10.core.stark_account import StarkPerpetualAccount
from x10.models.order import (
    OrderPriceType,
    OrderSide,
    OrderSortBy,
    OrderTpslType,
    OrderTriggerPriceType,
    OrderType,
    TimeInForce,
)
from x10.signing.order_object import OrderTpslTriggerParam, create_order_object
from x10.utils.order import get_price_with_slippage

from adapters.base import ExchangeAdapter, MarketPrice, Position, Side

# 统一 Side 与 SDK OrderSide 的映射
_SIDE_TO_SDK = {Side.BUY: OrderSide.BUY, Side.SELL: OrderSide.SELL}


def build_client_config(name: str):
    """构造 SDK 配置，并设置单次 HTTP 请求超时。

    超时定为 25 秒的依据（2026-08-10 实测）：
    - TCP 连接仅 40~50ms，链路本身不慢；
    - 复用 keep-alive 连接时单请求 0.4~1.3s；
    - 但连接重建时 TLS 握手要 0.5~3.1s，整请求 4~7.4s。
    旧值 10 秒对"重建连接"这条路径只有约 1.5 倍余量，抖动即超时，
    表现为整仓 TPSL 维护失败、当轮禁止新增风险仓（8/10 全天 27 次）。
    """
    config = get_config_by_name(name.upper())
    defaults = replace(config.defaults, request_timeout_seconds=25)
    return replace(config, defaults=defaults)


def filter_grid_orders(open_orders: list) -> list:
    """从开放订单里挑出"普通网格单"（该撤的），保留 TPSL / reduce_only 兜底单。"""
    out = []
    for o in open_orders:
        if bool(getattr(o, "reduce_only", False)):
            continue
        if str(getattr(o, "type", "") or "").upper() in ("TPSL", "CONDITIONAL"):
            continue
        out.append(o)
    return out


def _is_position_tpsl(order) -> bool:
    """判断订单是否为整仓 TPSL。"""
    order_type = str(getattr(order, "type", "") or "").upper()
    tp_sl_type = str(getattr(order, "tp_sl_type", "") or "").upper()
    return order_type == "TPSL" and tp_sl_type == "POSITION"


def filter_position_tpsl(orders: list):
    """从开放订单中返回第一张整仓 TPSL；不存在时返回 None。"""
    return next((order for order in orders if _is_position_tpsl(order)), None)


class ExtendedClient(ExchangeAdapter):
    """Extended（x10 / Starknet）对冲腿适配器（异步）。"""

    name = "extended"

    def __init__(self, rest_client: RestApiClient) -> None:
        self._client = rest_client
        self._markets: dict | None = None

    # ---- 构造 ----

    @classmethod
    def from_env(cls, prefix: str = "X10") -> "ExtendedClient":
        """从环境变量构造客户端，支持多账户前缀。

        prefix="X10"      → farm 对冲账户（X10_API_KEY 等）
        prefix="X10_GRID" → 网格账户（X10_GRID_API_KEY 等），与 farm 完全独立
        读取 {prefix}_CLIENT_CONFIG_NAME/API_KEY/PUBLIC_KEY/PRIVATE_KEY/VAULT_ID。
        """
        def g(key: str) -> str | None:
            return os.getenv(f"{prefix}_{key}")

        cfg_name = (g("CLIENT_CONFIG_NAME") or "TESTNET").upper()
        api_key, public, private, vault = g("API_KEY"), g("PUBLIC_KEY"), g("PRIVATE_KEY"), g("VAULT_ID")
        missing = [f"{prefix}_{k}" for k, v in
                   [("API_KEY", api_key), ("PUBLIC_KEY", public), ("PRIVATE_KEY", private), ("VAULT_ID", vault)]
                   if not v]
        if missing:
            raise RuntimeError(f"缺少环境变量：{missing}")
        stark_account = StarkPerpetualAccount(
            api_key=api_key,
            public_key=public.lower(),
            private_key=private.lower(),
            vault=int(vault),
        )
        rest = RestApiClient(build_client_config(cfg_name), stark_account)
        return cls(rest)

    async def connect(self) -> None:
        """加载并缓存市场元数据（下单需要）。"""
        await self._ensure_markets()

    async def _ensure_markets(self) -> None:
        """按需加载市场元数据，避免重复请求交易所。"""
        if self._markets is None:
            self._markets = await self._client.info.get_markets_dict()

    def _market(self, market_name: str):
        if self._markets is None:
            raise RuntimeError("请先 await connect() 加载市场元数据")
        if market_name not in self._markets:
            raise KeyError(f"Extended 不支持标的 {market_name}")
        return self._markets[market_name]

    def _round_qty(self, market: str, amount: Decimal) -> Decimal:
        """按标的数量步长取整（向下），并保证不低于最小下单量。"""
        tc = self._market(market).trading_config
        step = Decimal(str(tc.min_order_size_change))
        min_size = Decimal(str(tc.min_order_size))
        q = (Decimal(str(amount)) / step).to_integral_value(rounding=ROUND_DOWN) * step
        return q if q >= min_size else min_size

    # ---- 只读 ----

    async def get_market_price(self, market_name: str) -> MarketPrice:
        """获取买一/卖一价。"""
        stats = await self._client.info.get_market_statistics(market_name=market_name)
        return MarketPrice(
            market=market_name,
            bid=Decimal(str(stats.data.bid_price)),
            ask=Decimal(str(stats.data.ask_price)),
        )

    async def get_min_order_size(self, market: str) -> Decimal:
        """从交易所市场元数据读取最小下单量。"""
        await self._ensure_markets()
        return Decimal(str(self._market(market).trading_config.min_order_size))

    async def get_mark_price(self, market_name: str) -> Decimal:
        """标记价。

        必须覆盖基类默认（买卖中值）：中值与标记价是两个口径，而引擎的
        清算距离、硬止损和整仓 TPSL 都按标记价计算，用中值会让这些
        保护线整体偏移。
        """
        stats = await self._client.info.get_market_statistics(market_name=market_name)
        return Decimal(str(stats.data.mark_price))

    async def get_position(self, market_name: str) -> Position:
        """获取某标的当前持仓（无仓位返回 signed_size=0）。

        TODO(实盘确认): 核对 SDK 持仓模型的数量/方向字段名
        （此处按 size + side 归一化为有符号数量）。
        """
        resp = await self._client.account.get_positions(market_names=[market_name])
        items = resp.data or []
        if not items:
            return Position(market=market_name, signed_size=Decimal(0))
        return self._normalize_position(items[0], fallback_market=market_name)

    @staticmethod
    def _normalize_position(pos, *, fallback_market: str | None = None) -> Position:
        """把 Extended 原始持仓归一化为统一持仓快照。"""
        size = Decimal(str(pos.size))
        # side 为 SHORT 时取负；不同 SDK 版本可能用枚举或字符串，做兼容
        side = str(getattr(pos, "side", "")).upper()
        signed = -size if "SHORT" in side or "SELL" in side else size
        market = str(getattr(pos, "market", fallback_market) or fallback_market or "")
        if not market:
            raise RuntimeError("Extended 持仓缺少 market 字段")
        return Position(market=market, signed_size=signed, raw=pos)

    async def get_all_positions(self) -> list[Position]:
        """获取账户全部持仓，不按标的过滤。"""
        resp = await self._client.account.get_positions()
        return [self._normalize_position(pos) for pos in (resp.data or [])]

    async def get_balance(self):
        """账户余额（原始模型）。

        账户未入金时 /user/balance 可能 404，此时回退到 get_account（含账户状态/vault）。
        入金后应优先返回真正的余额模型。
        """
        try:
            resp = await self._client.account.get_balance()
            return resp.data
        except Exception:  # noqa: BLE001 未入金时 balance 端点 404，回退账户信息
            resp = await self._client.account.get_account()
            return resp.data

    async def get_liquidation_info(self, market: str) -> tuple[Decimal, Decimal] | None:
        """从持仓读取 (mark_price, liquidation_price)。Extended SDK 直接提供。"""
        resp = await self._client.account.get_positions(market_names=[market])
        items = resp.data or []
        if not items:
            return None
        p = items[0]
        liq = Decimal(str(getattr(p, "liquidation_price", 0) or 0))
        mark = Decimal(str(getattr(p, "mark_price", 0) or 0))
        if liq <= 0 or mark <= 0:
            return None
        return mark, liq

    async def get_free_margin_ratio(self) -> Decimal | None:
        """可用交易保证金 / 权益。Extended 是对冲的保证金瓶颈腿。"""
        bal = await self.get_balance()
        equity = Decimal(str(getattr(bal, "equity", 0) or 0))
        avail = Decimal(str(getattr(bal, "available_for_trade", 0) or 0))
        if equity <= 0:
            return None
        return avail / equity

    async def set_leverage(self, market_name: str, leverage: Decimal) -> None:
        """设置某标的杠杆。"""
        await self._client.account.update_leverage(market_name=market_name, leverage=leverage)

    # ---- 交易 ----

    async def market_order(
        self,
        market: str,
        side: Side,
        amount: Decimal,
        *,
        reduce_only: bool = False,
    ):
        """以 IOC 市价单开/平仓（带滑点保护）。"""
        sdk_side = _SIDE_TO_SDK[side]
        amount = self._round_qty(market, amount)
        market_obj = self._market(market)
        stats = await self._client.info.get_market_statistics(market_name=market)
        best = stats.data.ask_price if sdk_side == OrderSide.BUY else stats.data.bid_price
        price = get_price_with_slippage(
            side=sdk_side,
            price=best,
            min_price_change=market_obj.trading_config.min_price_change,
            slippage=self._client.config.defaults.market_price_slippage,
        )
        order = create_order_object(
            account=self._client.stark_account,
            order_type=OrderType.MARKET,
            starknet_domain=self._client.config.signing.starknet_domain,
            market=market_obj,
            side=sdk_side,
            amount_of_synthetic=amount,
            price=price,
            time_in_force=TimeInForce.IOC,
            reduce_only=reduce_only,
            post_only=False,
        )
        return await self._client.orders.place_order(order=order)

    # hedge / close_position 复用基类通用实现

    # ---- 限价单（网格用）----

    async def round_price(self, market: str, price: Decimal) -> Decimal:
        """复用 SDK 市场配置，按该市场价格 tick 对齐。"""
        trading_config = self._market(market).trading_config
        return Decimal(str(trading_config.round_price(Decimal(str(price)))))

    async def round_amount(self, market: str, amount: Decimal) -> Decimal:
        """复用 SDK 市场配置，按该市场数量步长对齐。"""
        return self._round_qty(market, amount)

    async def get_price_tick_size(self, market: str) -> Decimal:
        """返回 SDK 市场配置中的最小价格变动。"""
        return Decimal(str(self._market(market).trading_config.min_price_change))

    async def place_limit_order(
        self,
        market: str,
        side: Side,
        amount: Decimal,
        price: Decimal,
        *,
        post_only: bool = True,
        expire_days: int = 90,
        reduce_only: bool = False,
    ):
        """挂限价单（默认 post_only=maker）。返回下单结果（含订单 id）。

        SDK 默认有效期仅 1 小时，网格挂单会整批 EXPIRED（2026-07-20 实盘事故），
        这里显式设置为 expire_days 天。
        """
        amount = self._round_qty(market, amount)
        market_obj = self._market(market)
        order = create_order_object(
            account=self._client.stark_account,
            order_type=OrderType.LIMIT,
            starknet_domain=self._client.config.signing.starknet_domain,
            market=market_obj,
            side=_SIDE_TO_SDK[side],
            amount_of_synthetic=amount,
            price=market_obj.trading_config.round_price(price),
            time_in_force=TimeInForce.GTT,
            expire_time=datetime.now(timezone.utc) + timedelta(days=expire_days),
            reduce_only=reduce_only,
            post_only=post_only,
        )
        return await self._client.orders.place_order(order=order)

    async def place_position_stop_loss(
        self,
        market: str,
        signed_size: Decimal,
        trigger_price: Decimal,
    ):
        """为当前整仓挂 reduce-only 市价止损，多仓平多、空仓平空。"""
        signed_size = Decimal(str(signed_size))
        trigger_price = Decimal(str(trigger_price))
        if signed_size == 0:
            raise ValueError("空仓不能挂整仓止损")
        if trigger_price <= 0:
            raise ValueError("止损触发价必须大于 0")

        close_side = OrderSide.SELL if signed_size > 0 else OrderSide.BUY
        market_obj = self._market(market)
        trigger_price = await self.round_price(market, trigger_price)
        execution_price = get_price_with_slippage(
            side=close_side,
            price=trigger_price,
            min_price_change=market_obj.trading_config.min_price_change,
            slippage=self._client.config.defaults.market_price_slippage,
        )
        execution_price = await self.round_price(market, execution_price)
        order = create_order_object(
            account=self._client.stark_account,
            order_type=OrderType.TPSL,
            starknet_domain=self._client.config.signing.starknet_domain,
            market=market_obj,
            side=close_side,
            amount_of_synthetic=Decimal(0),
            price=Decimal(0),
            time_in_force=TimeInForce.GTT,
            expire_time=datetime.now(timezone.utc) + timedelta(days=90),
            reduce_only=True,
            post_only=False,
            tp_sl_type=OrderTpslType.POSITION,
            stop_loss=OrderTpslTriggerParam(
                trigger_price=trigger_price,
                trigger_price_type=OrderTriggerPriceType.MARK,
                price=execution_price,
                price_type=OrderPriceType.MARKET,
            ),
        )
        return await self._client.orders.place_order(order=order)

    async def cancel_order(self, market: str, order_id) -> None:
        """撤单。Extended 的订单号全局唯一，market 仅为满足统一契约。"""
        del market
        await self._client.orders.cancel_order(order_id=order_id)

    async def get_open_orders(self, market: str) -> list:
        r = await self._client.account.get_open_orders(market_names=[market])
        return r.data or []

    async def get_all_open_orders(self) -> list:
        """获取账户全部开放订单，不按标的过滤。"""
        r = await self._client.account.get_open_orders()
        return r.data or []

    async def cancel_grid_orders(self, market: str) -> int:
        """逐单撤掉普通网格单，保留 reduce-only、TPSL 与条件单。"""
        orders = filter_grid_orders(await self.get_open_orders(market))
        for order in orders:
            await self.cancel_order(market, order.id)
        return len(orders)

    async def cancel_tpsl(self, market: str) -> None:
        """逐单撤掉该市场的整仓 TPSL，不影响其他订单。"""
        orders = await self.get_open_orders(market)
        for order in orders:
            if _is_position_tpsl(order):
                await self.cancel_order(market, order.id)

    async def get_position_tpsl(self, market: str):
        """查询当前市场真实挂出的整仓 TPSL。"""
        return filter_position_tpsl(await self.get_open_orders(market))

    async def get_orders_history(
        self,
        market: str,
        limit: int = 100,
        *,
        order_type: str | OrderType | None = None,
        sort: str | OrderSortBy | None = None,
    ) -> list:
        """历史订单（含终态 status/filled_qty，网格判断成交 vs 过期/被撤用）。"""
        sdk_order_type = OrderType(order_type) if order_type is not None else None
        sdk_sort = OrderSortBy(sort) if sort is not None else None
        r = await self._client.account.get_orders_history(
            market_names=[market],
            order_type=sdk_order_type,
            limit=limit,
            sort=sdk_sort,
        )
        return r.data or []

    async def get_order_by_id(self, market: str, order_id):
        """按 ID 查询单笔订单；market 保留用于统一适配器接口。"""
        del market
        r = await self._client.account.get_order_by_id(order_id=order_id)
        return r.data

    async def close(self) -> None:
        await self._client.close()
