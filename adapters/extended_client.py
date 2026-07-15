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

from dataclasses import dataclass
from decimal import Decimal

from x10.clients.rest.rest_api_client import RestApiClient
from x10.config import get_config_by_name
from x10.core.env_config import EnvConfig
from x10.core.stark_account import StarkPerpetualAccount
from x10.models.order import OrderSide, OrderType, TimeInForce
from x10.signing.order_object import create_order_object
from x10.utils.order import get_price_with_slippage


@dataclass
class MarketPrice:
    """某标的的买一/卖一价。"""

    market: str
    bid: Decimal
    ask: Decimal

    @property
    def mid(self) -> Decimal:
        return (self.bid + self.ask) / 2


@dataclass
class Position:
    """持仓快照（方向已归一化：正=多，负=空）。"""

    market: str
    signed_size: Decimal  # 有符号数量：>0 多头，<0 空头，0 无仓位
    raw: object = None    # 原始 SDK 模型，便于取更多字段


class ExtendedClient:
    """Extended 对冲腿适配器（异步）。"""

    def __init__(self, rest_client: RestApiClient) -> None:
        self._client = rest_client
        self._markets: dict | None = None

    # ---- 构造 ----

    @classmethod
    def from_env(cls) -> "ExtendedClient":
        """从环境变量（X10_*）构造客户端。"""
        env = EnvConfig.parse()
        env.validate_private_api_credentials()
        stark_account = StarkPerpetualAccount(
            api_key=env.api_key,
            public_key=env.public_key,
            private_key=env.private_key,
            vault=env.vault_id,
        )
        rest = RestApiClient(get_config_by_name(env.client_config_name), stark_account)
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
        """账户余额（原始模型，字段待实盘确认）。"""
        resp = await self._client.account.get_balance()
        return resp.data

    async def set_leverage(self, market_name: str, leverage: Decimal) -> None:
        """设置某标的杠杆。"""
        await self._client.account.update_leverage(market_name=market_name, leverage=leverage)

    # ---- 交易 ----

    async def market_order(
        self,
        market_name: str,
        side: OrderSide,
        amount: Decimal,
        *,
        reduce_only: bool = False,
    ):
        """以 IOC 市价单开/平仓（带滑点保护）。"""
        market = self._market(market_name)
        stats = await self._client.info.get_market_statistics(market_name=market_name)
        best = stats.data.ask_price if side == OrderSide.BUY else stats.data.bid_price
        price = get_price_with_slippage(
            side=side,
            price=best,
            min_price_change=market.trading_config.min_price_change,
            slippage=self._client.config.defaults.market_price_slippage,
        )
        order = create_order_object(
            account=self._client.stark_account,
            order_type=OrderType.MARKET,
            starknet_domain=self._client.config.signing.starknet_domain,
            market=market,
            side=side,
            amount_of_synthetic=amount,
            price=price,
            time_in_force=TimeInForce.IOC,
            reduce_only=reduce_only,
            post_only=False,
        )
        return await self._client.orders.place_order(order=order)

    async def hedge(self, market_name: str, target_signed_size: Decimal):
        """把某标的持仓调整到目标有符号数量（对冲引擎再平衡用）。

        target_signed_size > 0 表示目标净多，< 0 净空。返回调整用的下单结果或 None。
        """
        current = await self.get_position(market_name)
        delta = target_signed_size - current.signed_size
        if delta == 0:
            return None
        side = OrderSide.BUY if delta > 0 else OrderSide.SELL
        # 缩小方向（reduce_only）与扩大方向区分，避免误开反向仓
        reduce_only = abs(target_signed_size) < abs(current.signed_size) and (
            target_signed_size * current.signed_size >= 0
        )
        return await self.market_order(
            market_name, side, abs(delta), reduce_only=reduce_only
        )

    async def close_position(self, market_name: str):
        """市价平掉某标的全部仓位。"""
        pos = await self.get_position(market_name)
        if pos.signed_size == 0:
            return None
        side = OrderSide.SELL if pos.signed_size > 0 else OrderSide.BUY
        return await self.market_order(
            market_name, side, abs(pos.signed_size), reduce_only=True
        )

    async def close(self) -> None:
        await self._client.close()
