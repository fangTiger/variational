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
from decimal import Decimal

from x10.clients.rest.rest_api_client import RestApiClient
from x10.config import get_config_by_name
from x10.core.stark_account import StarkPerpetualAccount
from x10.models.order import OrderSide, OrderType, TimeInForce
from x10.signing.order_object import create_order_object
from x10.utils.order import get_price_with_slippage

from adapters.base import ExchangeAdapter, MarketPrice, Position, Side

# 统一 Side 与 SDK OrderSide 的映射
_SIDE_TO_SDK = {Side.BUY: OrderSide.BUY, Side.SELL: OrderSide.SELL}


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
        rest = RestApiClient(get_config_by_name(cfg_name), stark_account)
        return cls(rest)

    async def connect(self) -> None:
        """加载并缓存市场元数据（下单需要）。"""
        self._markets = await self._client.info.get_markets_dict()

    def _market(self, market_name: str):
        if self._markets is None:
            raise RuntimeError("请先 await connect() 加载市场元数据")
        if market_name not in self._markets:
            raise KeyError(f"Extended 不支持标的 {market_name}")
        return self._markets[market_name]

    # ---- 只读 ----

    async def get_market_price(self, market_name: str) -> MarketPrice:
        """获取买一/卖一价。"""
        stats = await self._client.info.get_market_statistics(market_name=market_name)
        return MarketPrice(
            market=market_name,
            bid=Decimal(str(stats.data.bid_price)),
            ask=Decimal(str(stats.data.ask_price)),
        )

    async def get_position(self, market_name: str) -> Position:
        """获取某标的当前持仓（无仓位返回 signed_size=0）。

        TODO(实盘确认): 核对 SDK 持仓模型的数量/方向字段名
        （此处按 size + side 归一化为有符号数量）。
        """
        resp = await self._client.account.get_positions(market_names=[market_name])
        items = resp.data or []
        if not items:
            return Position(market=market_name, signed_size=Decimal(0))
        pos = items[0]
        size = Decimal(str(pos.size))
        # side 为 SHORT 时取负；不同 SDK 版本可能用枚举或字符串，做兼容
        side = str(getattr(pos, "side", "")).upper()
        signed = -size if "SHORT" in side or "SELL" in side else size
        return Position(market=market_name, signed_size=signed, raw=pos)

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

    async def place_limit_order(self, market: str, side: Side, amount: Decimal, price: Decimal,
                                *, post_only: bool = True):
        """挂限价单（默认 post_only=maker）。返回下单结果（含订单 id）。"""
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
            reduce_only=False,
            post_only=post_only,
        )
        return await self._client.orders.place_order(order=order)

    async def cancel_order(self, order_id) -> None:
        await self._client.orders.cancel_order(order_id=order_id)

    async def get_open_orders(self, market: str) -> list:
        r = await self._client.account.get_open_orders(market_names=[market])
        return r.data or []

    async def close(self) -> None:
        await self._client.close()
