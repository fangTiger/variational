"""Hyperliquid 永续交易适配器，默认只读。

官方 ``hyperliquid-python-sdk`` 使用 EVM 私钥完成 EIP-712 签名。本适配器
优先且默认只支持 API 代理钱包（agent wallet）：主钱包只需提供公开地址，
主钱包私钥不应写入环境变量或落盘。

环境变量（``from_env`` 默认前缀为 ``HYPERLIQUID``）：

``HYPERLIQUID_ACCOUNT_ADDRESS``
    授权 API 代理钱包的主账户公开地址。账户读取需要；开启交易时必须提供。
``HYPERLIQUID_AGENT_PRIVATE_KEY``
    已由上述账户授权的 API 代理钱包私钥。仅开启交易时需要。
``HYPERLIQUID_API_URL``
    可选，默认使用 SDK 的主网地址。
``HYPERLIQUID_BUILDER_ADDRESS``
    可选 Builder Code 地址；必须与费率同时配置。
``HYPERLIQUID_BUILDER_FEE_TENTHS_BPS``
    可选 Builder Code 费率，整数，单位为 0.1bp；必须与地址同时配置。

``trading_enabled`` 必须由调用方显式传入，不从环境变量自动开启。无地址、无
私钥时仍可构造只读实例并读取公共行情；调用账户方法前需补充公开地址。
"""

from __future__ import annotations

import asyncio
import os
import re
from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_DOWN, ROUND_HALF_EVEN, Decimal
from typing import Any, Callable

from eth_account import Account
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from hyperliquid.utils.constants import MAINNET_API_URL

from adapters.base import ExchangeAdapter, MarketPrice, Position, Side
from infra.logger import get_logger

logger = get_logger("hyperliquid_client")

DEFAULT_TIMEOUT = 10.0
DEFAULT_MARKET_ORDER_SLIPPAGE = Decimal("0.05")
# Hyperliquid 永续当前按最小 10 USDC 名义额校验，SDK 元数据不返回数量下限。
MIN_ORDER_NOTIONAL_USD = Decimal("10")
PERP_MAX_PRICE_DECIMALS = 6
MAX_PRICE_SIGNIFICANT_DIGITS = 5
# Hyperliquid 永续 builder 费率上限为 10bp，即 100 个 0.1bp。
MAX_BUILDER_FEE_TENTHS_BPS = 100


@dataclass(frozen=True)
class HyperliquidBalance:
    """供通用引擎读取的统一账户权益及其组成。"""

    equity: Decimal
    withdrawable: Decimal
    perps_account_value: Decimal = Decimal(0)
    spot_usdc_total: Decimal = Decimal(0)
    unrealized_pnl_total: Decimal = Decimal(0)


@dataclass(frozen=True)
class HyperliquidOrderResult:
    """下单结果视图；``id`` 对齐引擎统一取值方式。"""

    id: int
    status: str
    raw: dict[str, Any]


@dataclass(frozen=True)
class HyperliquidOrder:
    """把 Hyperliquid 字典订单归一化为引擎可读的属性视图。"""

    id: int
    market: str
    status: str
    side: str
    price: Decimal
    qty: Decimal
    filled_qty: Decimal
    reduce_only: bool
    type: str
    trigger_price: Decimal
    created_at: int
    updated_at: int
    raw: dict[str, Any]

    @classmethod
    def from_open(cls, raw: dict[str, Any]) -> "HyperliquidOrder":
        """从 ``frontend_open_orders`` 的完整元素构造活动订单。"""
        return cls._from_raw(raw, status="OPEN", updated_at=raw.get("timestamp"))

    @classmethod
    def from_history(cls, raw: dict[str, Any]) -> "HyperliquidOrder":
        """从 ``historical_orders`` 或 ``query_order_by_oid`` 元素构造。"""
        if not isinstance(raw, dict) or not isinstance(raw.get("order"), dict):
            raise ValueError(f"Hyperliquid 历史订单结构无效：{raw!r}")
        return cls._from_raw(
            raw["order"],
            status=_normalize_order_status(raw.get("status")),
            updated_at=raw.get("statusTimestamp"),
            envelope=raw,
        )

    @classmethod
    def _from_raw(
        cls,
        raw: dict[str, Any],
        *,
        status: str,
        updated_at: Any,
        envelope: dict[str, Any] | None = None,
    ) -> "HyperliquidOrder":
        """校验关键字段并完成统一字段映射。"""
        if not isinstance(raw, dict):
            raise ValueError(f"Hyperliquid 订单必须是对象：{raw!r}")
        for key in ("coin", "oid", "side", "limitPx", "sz"):
            if raw.get(key) is None:
                raise ValueError(f"Hyperliquid 订单缺少字段 {key}：{raw!r}")

        side_code = str(raw["side"]).upper()
        if side_code not in ("A", "B"):
            raise ValueError(f"Hyperliquid 订单方向无效：{raw['side']!r}")

        remaining = _decimal(raw["sz"], context="订单剩余数量", non_negative=True)
        qty = _decimal(
            raw.get("origSz", raw["sz"]),
            context="订单原始数量",
            non_negative=True,
        )
        if remaining > qty:
            raise ValueError(f"Hyperliquid 订单剩余数量大于原始数量：{raw!r}")

        raw_for_audit = envelope if envelope is not None else raw
        return cls(
            id=int(raw["oid"]),
            market=str(raw["coin"]),
            status=_normalize_order_status(status),
            side=(Side.SELL if side_code == "A" else Side.BUY).value,
            price=_decimal(raw["limitPx"], context="订单价格", non_negative=True),
            qty=qty,
            filled_qty=qty - remaining,
            reduce_only=bool(raw.get("reduceOnly", False)),
            type=str(raw.get("orderType") or "").upper(),
            trigger_price=_decimal(
                raw.get("triggerPx", 0),
                context="订单触发价",
                non_negative=True,
            ),
            created_at=int(raw.get("timestamp") or 0),
            updated_at=int(updated_at or raw.get("timestamp") or 0),
            raw=raw_for_audit,
        )


@dataclass(frozen=True)
class _MarketMeta:
    """永续标的精度元数据。"""

    coin: str
    sz_decimals: int


def _decimal(value: Any, *, context: str, non_negative: bool = False) -> Decimal:
    """严格解析有限十进制数，拒绝 NaN、Infinity 与非法负数。"""
    try:
        result = Decimal(str(value))
    except Exception as exc:  # noqa: BLE001 需要统一补充字段上下文
        raise ValueError(f"Hyperliquid {context}不是有效数字：{value!r}") from exc
    if not result.is_finite():
        raise ValueError(f"Hyperliquid {context}必须是有限数字：{value!r}")
    if non_negative and result < 0:
        raise ValueError(f"Hyperliquid {context}不得为负：{value!r}")
    return result


def _positive_decimal(value: Any, *, context: str) -> Decimal:
    """解析正十进制数。"""
    result = _decimal(value, context=context)
    if result <= 0:
        raise ValueError(f"Hyperliquid {context}必须为正数：{value!r}")
    return result


def _normalize_order_status(value: Any) -> str:
    """把交易所订单状态归一化为引擎使用的终态词汇。"""
    status = str(value or "").strip().upper().replace("-", "_")
    if status.endswith("CANCELED") or status.endswith("CANCELLED"):
        return "CANCELLED"
    return status


def _builder_info(
    address: str | None,
    fee_tenths_bps: int | str | None,
) -> dict[str, Any] | None:
    """校验可选 builder 配置并生成 SDK 0.24.0 所需的 ``b/f`` 对象。"""
    if address is None and fee_tenths_bps is None:
        return None
    if address is None or fee_tenths_bps is None:
        raise ValueError("Hyperliquid builder 地址与费率必须同时配置")
    if not isinstance(address, str) or re.fullmatch(r"0x[0-9a-fA-F]{40}", address) is None:
        raise ValueError("Hyperliquid builder 地址格式无效，必须是 0x 加 40 位十六进制字符")

    if isinstance(fee_tenths_bps, bool):
        raise ValueError("Hyperliquid builder 费率必须是整数")
    if isinstance(fee_tenths_bps, int):
        fee = fee_tenths_bps
    elif isinstance(fee_tenths_bps, str) and re.fullmatch(
        r"[+-]?\d+", fee_tenths_bps.strip()
    ):
        fee_text = fee_tenths_bps.strip()
        fee_digits = fee_text.lstrip("+-").lstrip("0") or "0"
        if len(fee_digits) > len(str(MAX_BUILDER_FEE_TENTHS_BPS)):
            fee = -1 if fee_text.startswith("-") else MAX_BUILDER_FEE_TENTHS_BPS + 1
        else:
            fee = int(fee_digits)
            if fee_text.startswith("-"):
                fee = -fee
    else:
        raise ValueError("Hyperliquid builder 费率必须是整数")
    if fee < 0:
        raise ValueError("Hyperliquid builder 费率不得为负")
    if fee > MAX_BUILDER_FEE_TENTHS_BPS:
        raise ValueError(
            "Hyperliquid builder 费率超过永续上限 "
            f"{MAX_BUILDER_FEE_TENTHS_BPS}（单位 0.1bp）"
        )
    return {"b": address, "f": fee}


class HyperliquidClient(ExchangeAdapter):
    """Hyperliquid 永续适配器，构造时默认只读且不访问网络。

    凭据约定详见模块文档。生产交易实例应传入 API 代理钱包私钥；
    ``exchange`` 与工厂参数仅用于依赖装配和无网络契约测试。
    """

    name = "hyperliquid"

    def __init__(
        self,
        account_address: str | None = None,
        *,
        base_url: str = MAINNET_API_URL,
        timeout: float = DEFAULT_TIMEOUT,
        trading_enabled: bool = False,
        agent_private_key: str | None = None,
        info: Any | None = None,
        exchange: Any | None = None,
        info_factory: Callable[..., Any] = Info,
        exchange_factory: Callable[..., Any] = Exchange,
        market_order_slippage: Decimal = DEFAULT_MARKET_ORDER_SLIPPAGE,
        builder_address: str | None = None,
        builder_fee_tenths_bps: int | str | None = None,
    ) -> None:
        """保存配置但不联网；交易模式必须具备账户地址与代理签名能力。"""
        if trading_enabled and not account_address:
            raise ValueError("Hyperliquid 开启交易时必须提供主账户地址")
        if trading_enabled and exchange is None and not agent_private_key:
            raise ValueError("Hyperliquid 开启交易时必须提供 API 代理钱包私钥")

        slippage = _decimal(market_order_slippage, context="市价单滑点")
        if slippage < 0 or slippage >= 1:
            raise ValueError("Hyperliquid 市价单滑点必须在 [0, 1) 之间")
        builder = _builder_info(builder_address, builder_fee_tenths_bps)

        self.account_address = account_address
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.trading_enabled = trading_enabled
        self._agent_private_key = agent_private_key
        self._info = info
        self._exchange = exchange
        self._info_factory = info_factory
        self._exchange_factory = exchange_factory
        self._market_order_slippage = slippage
        self._builder = builder
        self._market_meta: dict[str, _MarketMeta] = {}
        self._market_aliases: dict[str, str] = {}

    @classmethod
    def from_env(
        cls,
        prefix: str = "HYPERLIQUID",
        *,
        trading_enabled: bool = False,
        **kwargs,
    ) -> "HyperliquidClient":
        """从环境变量构造，且绝不从环境变量隐式开启交易。

        读取 ``{prefix}_ACCOUNT_ADDRESS``、``{prefix}_AGENT_PRIVATE_KEY``、
        可选的 ``{prefix}_API_URL`` 以及成对可选的 builder 地址和费率。只读
        公共行情不要求账户凭据；交易模式同时要求公开地址与代理钱包私钥。
        """
        return cls(
            account_address=os.getenv(f"{prefix}_ACCOUNT_ADDRESS") or None,
            agent_private_key=os.getenv(f"{prefix}_AGENT_PRIVATE_KEY") or None,
            base_url=os.getenv(f"{prefix}_API_URL") or MAINNET_API_URL,
            builder_address=os.getenv(f"{prefix}_BUILDER_ADDRESS") or None,
            builder_fee_tenths_bps=(
                os.getenv(f"{prefix}_BUILDER_FEE_TENTHS_BPS") or None
            ),
            trading_enabled=trading_enabled,
            **kwargs,
        )

    @property
    def supports_trading(self) -> bool:
        """与显式交易开关同源，供引擎判断是否允许自动交易。"""
        return self.trading_enabled

    async def connect(self) -> None:
        """创建 SDK 读取客户端、加载元数据，并按需装配代理钱包交易客户端。"""
        if self._info is None:
            self._info = await asyncio.to_thread(
                self._info_factory,
                base_url=self.base_url,
                skip_ws=True,
                timeout=self.timeout,
            )

        meta, _contexts = await self._refresh_market_meta()
        if self.trading_enabled and self._exchange is None:
            try:
                wallet = Account.from_key(self._agent_private_key)
            except Exception as exc:  # noqa: BLE001 SDK 的密钥异常类型不稳定
                raise ValueError("Hyperliquid API 代理钱包私钥无效") from exc
            # 只交易永续；传空 spot 元数据可避免 Exchange 再发一次 spotMeta 请求。
            self._exchange = await asyncio.to_thread(
                self._exchange_factory,
                wallet,
                base_url=self.base_url,
                meta=meta,
                account_address=self.account_address,
                spot_meta={"tokens": [], "universe": []},
                timeout=self.timeout,
            )

    def _require_info(self):
        """返回读取客户端；未连接时给出明确错误。"""
        if self._info is None:
            raise RuntimeError("请先 await connect() 初始化 Hyperliquid 只读客户端")
        return self._info

    def _require_account_address(self) -> str:
        """账户读取只需公开地址，不要求任何私钥。"""
        if not self.account_address:
            raise RuntimeError("Hyperliquid 账户读取需要 ACCOUNT_ADDRESS 公开地址")
        return self.account_address

    def _require_trading(self):
        """所有写操作必须先通过显式交易开关。"""
        if not self.trading_enabled:
            raise PermissionError(
                "该 HyperliquidClient 实例是只读的，禁止下单撤单；"
                "需以 trading_enabled=True 显式构造交易实例"
            )
        if self._exchange is None:
            raise RuntimeError("请先 await connect() 装配 Hyperliquid 代理钱包交易客户端")
        return self._exchange

    async def _refresh_market_meta(self) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """读取并严格校验 ``metaAndAssetCtxs``，同时刷新精度缓存。"""
        raw = await asyncio.to_thread(self._require_info().meta_and_asset_ctxs)
        if not isinstance(raw, (list, tuple)) or len(raw) != 2:
            raise ValueError("Hyperliquid metaAndAssetCtxs 响应结构无效")
        meta, contexts = raw
        if not isinstance(meta, dict) or not isinstance(meta.get("universe"), list):
            raise ValueError("Hyperliquid 元数据缺少 universe 数组")
        if not isinstance(contexts, list) or len(contexts) != len(meta["universe"]):
            raise ValueError("Hyperliquid 行情上下文与 universe 长度不一致")

        market_meta: dict[str, _MarketMeta] = {}
        aliases: dict[str, str] = {}
        for item in meta["universe"]:
            if not isinstance(item, dict) or item.get("name") is None:
                raise ValueError(f"Hyperliquid universe 元素无效：{item!r}")
            coin = str(item["name"])
            try:
                sz_decimals = int(item["szDecimals"])
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError(f"Hyperliquid {coin} 缺少有效 szDecimals") from exc
            if not 0 <= sz_decimals <= PERP_MAX_PRICE_DECIMALS:
                raise ValueError(f"Hyperliquid {coin} szDecimals 超出永续范围")
            market_meta[coin] = _MarketMeta(coin=coin, sz_decimals=sz_decimals)
            aliases[coin.upper()] = coin
            if ":" not in coin:
                aliases[f"{coin.upper()}-USD"] = coin
                aliases[f"{coin.upper()}/USD"] = coin

        self._market_meta = market_meta
        self._market_aliases = aliases
        return meta, contexts

    async def _market(self, market: str) -> _MarketMeta:
        """按实际交易所名称解析标的，兼容项目常用的 ``BTC-USD`` 别名。"""
        alias = str(market).upper()
        coin = self._market_aliases.get(alias)
        if coin is None:
            await self._refresh_market_meta()
            coin = self._market_aliases.get(alias)
        if coin is None:
            raise KeyError(f"Hyperliquid 没有标的 {market}")
        return self._market_meta[coin]

    async def get_market_price(self, market: str) -> MarketPrice:
        """返回交易所 L2 快照中的真实买一卖一，拒绝无效或交叉盘口。"""
        coin = (await self._market(market)).coin
        raw = await asyncio.to_thread(self._require_info().l2_snapshot, coin)
        if not isinstance(raw, dict) or not isinstance(raw.get("levels"), list):
            raise ValueError("Hyperliquid 盘口响应缺少 levels")
        levels = raw["levels"]
        if len(levels) != 2 or not all(isinstance(level, list) and level for level in levels):
            raise ValueError("Hyperliquid 盘口缺少买一或卖一")
        if not all(isinstance(item, dict) for side in levels for item in side):
            raise ValueError("Hyperliquid 盘口档位结构无效")

        bids = [
            _positive_decimal(item.get("px"), context="盘口买盘价格")
            for item in levels[0]
        ]
        asks = [
            _positive_decimal(item.get("px"), context="盘口卖盘价格")
            for item in levels[1]
        ]
        bid, ask = max(bids), min(asks)
        if bid >= ask:
            raise ValueError(f"Hyperliquid 盘口无效：bid={bid} 必须小于 ask={ask}")
        return MarketPrice(market=market, bid=bid, ask=ask)

    async def get_mark_price(self, market: str) -> Decimal:
        """读取交易所 ``markPx``；绝不使用盘口中值替代标记价。"""
        meta, contexts = await self._refresh_market_meta()
        target = (await self._market(market)).coin
        for item, context in zip(meta["universe"], contexts, strict=True):
            if str(item["name"]) != target:
                continue
            if not isinstance(context, dict) or context.get("markPx") is None:
                raise ValueError(f"Hyperliquid {target} 行情上下文缺少 markPx")
            return _positive_decimal(context["markPx"], context=f"{target} 标记价")
        raise KeyError(f"Hyperliquid 没有标的 {market}")

    async def _user_state(self) -> dict[str, Any]:
        """读取账户状态，结构异常时不得伪装成空账户。"""
        raw = await asyncio.to_thread(
            self._require_info().user_state,
            self._require_account_address(),
        )
        if not isinstance(raw, dict) or not isinstance(raw.get("assetPositions"), list):
            raise ValueError("Hyperliquid 账户状态缺少 assetPositions 数组")
        return raw

    async def _spot_user_state(self) -> dict[str, Any]:
        """读取 Spot 账户状态，严格校验 SDK 返回的余额数组。"""
        raw = await asyncio.to_thread(
            self._require_info().spot_user_state,
            self._require_account_address(),
        )
        if not isinstance(raw, dict) or not isinstance(raw.get("balances"), list):
            raise ValueError("Hyperliquid Spot 账户状态缺少 balances 数组")
        if not all(isinstance(item, dict) for item in raw["balances"]):
            raise ValueError("Hyperliquid Spot 账户余额元素结构无效")
        return raw

    @staticmethod
    def _normalize_position(raw: dict[str, Any]) -> Position:
        """``szi`` 本身带方向，直接归一化为统一有符号数量。"""
        if not isinstance(raw, dict) or not isinstance(raw.get("position"), dict):
            raise ValueError(f"Hyperliquid 持仓结构无效：{raw!r}")
        position = raw["position"]
        if position.get("coin") is None or position.get("szi") is None:
            raise ValueError(f"Hyperliquid 持仓缺少 coin/szi：{raw!r}")
        signed_size = _decimal(position["szi"], context="持仓数量")
        return Position(market=str(position["coin"]), signed_size=signed_size, raw=raw)

    async def get_all_positions(self) -> list[Position]:
        """返回账户全部持仓，不按标的过滤。"""
        state = await self._user_state()
        return [self._normalize_position(raw) for raw in state["assetPositions"]]

    async def get_position(self, market: str) -> Position:
        """返回指定标的有符号持仓；未持有时返回零。"""
        target = (await self._market(market)).coin
        for position in await self.get_all_positions():
            if position.market == target:
                return Position(market=market, signed_size=position.signed_size, raw=position.raw)
        return Position(market=market, signed_size=Decimal(0))

    async def get_balance(self) -> HyperliquidBalance:
        """返回全平口径权益：Spot USDC 加全部持仓未实现盈亏。"""
        spot_usdc_total = Decimal(0)
        try:
            state = await self._user_state()
        except Exception as exc:  # noqa: BLE001 SDK 查询异常类型不稳定
            raise RuntimeError(f"Hyperliquid 永续账户状态查询失败：{exc}") from exc

        summary = state.get("marginSummary")
        if not isinstance(summary, dict) or summary.get("accountValue") is None:
            raise ValueError("Hyperliquid 账户状态缺少 marginSummary.accountValue")
        perps_account_value = _decimal(
            summary["accountValue"],
            context="永续账户权益诊断值",
        )
        withdrawable = _decimal(
            state.get("withdrawable"),
            context="可提现余额",
            non_negative=True,
        )
        unrealized_pnl_total = Decimal(0)
        for raw_position in state["assetPositions"]:
            if not isinstance(raw_position, dict) or not isinstance(
                raw_position.get("position"), dict
            ):
                raise ValueError(f"Hyperliquid 持仓结构无效：{raw_position!r}")
            position = raw_position["position"]
            if position.get("unrealizedPnl") is None:
                raise ValueError(f"Hyperliquid 持仓缺少未实现盈亏：{raw_position!r}")
            unrealized_pnl_total += _decimal(
                position["unrealizedPnl"],
                context="持仓未实现盈亏",
            )

        try:
            spot_state = await self._spot_user_state()
        except Exception as exc:  # noqa: BLE001 SDK 查询异常类型不稳定
            raise RuntimeError(f"Hyperliquid Spot 账户状态查询失败：{exc}") from exc
        for item in spot_state["balances"]:
            if item.get("coin") != "USDC":
                continue
            if item.get("total") is None:
                raise ValueError("Hyperliquid Spot USDC 余额缺少 total")
            spot_usdc_total += _decimal(
                item["total"],
                context="Spot USDC 余额",
                non_negative=True,
            )

        equity = _positive_decimal(
            spot_usdc_total + unrealized_pnl_total,
            context="账户权益合计",
        )
        return HyperliquidBalance(
            equity=equity,
            withdrawable=withdrawable,
            perps_account_value=perps_account_value,
            spot_usdc_total=spot_usdc_total,
            unrealized_pnl_total=unrealized_pnl_total,
        )

    async def get_free_margin_ratio(self) -> Decimal | None:
        """用可提现余额近似可用保证金占权益比例。"""
        balance = await self.get_balance()
        return balance.withdrawable / balance.equity

    async def get_liquidation_info(self, market: str) -> tuple[Decimal, Decimal] | None:
        """返回交易所标记价与持仓清算价；空仓或无清算价返回 ``None``。"""
        position = await self.get_position(market)
        if position.is_flat or not isinstance(position.raw, dict):
            return None
        raw_position = position.raw.get("position")
        if not isinstance(raw_position, dict) or not raw_position.get("liquidationPx"):
            return None
        liquidation_price = _decimal(
            raw_position["liquidationPx"],
            context="清算价",
            non_negative=True,
        )
        if liquidation_price == 0:
            return None
        return await self.get_mark_price(market), liquidation_price

    @staticmethod
    def _normalize_open_orders(raw: Any) -> list[HyperliquidOrder]:
        """严格解析活动订单数组。"""
        if not isinstance(raw, list):
            raise ValueError("Hyperliquid 活动订单响应不是数组")
        return [HyperliquidOrder.from_open(item) for item in raw]

    async def get_all_open_orders(self) -> list[HyperliquidOrder]:
        """返回账户全部活动订单，不按标的过滤。"""
        raw = await asyncio.to_thread(
            self._require_info().frontend_open_orders,
            self._require_account_address(),
        )
        return self._normalize_open_orders(raw)

    async def get_open_orders(self, market: str) -> list[HyperliquidOrder]:
        """返回指定标的活动订单。"""
        target = (await self._market(market)).coin
        return [order for order in await self.get_all_open_orders() if order.market == target]

    async def get_orders_history(
        self,
        market: str,
        limit: int = 100,
        *,
        order_type: str | None = None,
        sort: str | None = None,
    ) -> list[HyperliquidOrder]:
        """查询账户历史订单，在本地按标的、类型、更新时间与数量过滤。"""
        if limit < 0:
            raise ValueError("Hyperliquid 历史订单 limit 不得为负")
        target = (await self._market(market)).coin
        raw = await asyncio.to_thread(
            self._require_info().historical_orders,
            self._require_account_address(),
        )
        if not isinstance(raw, list):
            raise ValueError("Hyperliquid 历史订单响应不是数组")
        orders = [HyperliquidOrder.from_history(item) for item in raw]
        orders = [order for order in orders if order.market == target]
        if order_type is not None:
            wanted = str(order_type).upper().rsplit(".", 1)[-1]
            orders = [order for order in orders if order.type == wanted]
        if sort is not None and str(sort).upper().rsplit(".", 1)[-1] == "UPDATED_AT":
            orders.sort(key=lambda order: order.updated_at, reverse=True)
        return orders[:limit]

    async def get_order_by_id(
        self,
        market: str,
        order_id,
    ) -> HyperliquidOrder | None:
        """按交易所 OID 查询；未命中返回 ``None``，底层异常原样上抛。"""
        del market
        raw = await asyncio.to_thread(
            self._require_info().query_order_by_oid,
            self._require_account_address(),
            int(order_id),
        )
        if not isinstance(raw, dict):
            raise ValueError("Hyperliquid 订单查询响应不是对象")
        status = str(raw.get("status") or "")
        if status == "unknownOid":
            return None
        if status == "order" and isinstance(raw.get("order"), dict):
            return HyperliquidOrder.from_history(raw["order"])
        # 兼容直接返回历史订单元素的 SDK/桩形态，但不把畸形响应当作未命中。
        if isinstance(raw.get("order"), dict) and raw.get("statusTimestamp") is not None:
            return HyperliquidOrder.from_history(raw)
        raise ValueError(f"Hyperliquid 订单查询响应结构无效：{raw!r}")

    async def round_amount(self, market: str, amount: Decimal) -> Decimal:
        """按 ``szDecimals`` 向下取整，避免扩大请求数量与风险。"""
        meta = await self._market(market)
        value = _decimal(amount, context="订单数量", non_negative=True)
        quantum = Decimal(1).scaleb(-meta.sz_decimals)
        return value.quantize(quantum, rounding=ROUND_DOWN)

    @staticmethod
    def _price_quantum(price: Decimal, sz_decimals: int) -> Decimal:
        """计算当前价位满足五位有效数字与最大小数位的真实 tick。"""
        significant_exponent = price.adjusted() - (MAX_PRICE_SIGNIFICANT_DIGITS - 1)
        decimal_exponent = -(PERP_MAX_PRICE_DECIMALS - sz_decimals)
        # Hyperliquid 允许任意整数价，因此 tick 最大不超过 1。
        exponent = min(0, max(significant_exponent, decimal_exponent))
        return Decimal(1).scaleb(exponent)

    async def round_price(self, market: str, price: Decimal) -> Decimal:
        """按永续价格的五位有效数字和 ``6-szDecimals`` 小数位取整。"""
        meta = await self._market(market)
        value = _positive_decimal(price, context="订单价格")
        return value.quantize(
            self._price_quantum(value, meta.sz_decimals),
            rounding=ROUND_HALF_EVEN,
        )

    async def get_price_tick_size(self, market: str) -> Decimal:
        """按当前标记价返回实际有效 tick；Hyperliquid tick 随价位变化。"""
        meta = await self._market(market)
        mark_price = await self.get_mark_price(market)
        return self._price_quantum(mark_price, meta.sz_decimals)

    async def get_min_order_size(self, market: str) -> Decimal:
        """把 10 USDC 最小名义额按当前标记价换算并向上对齐数量精度。"""
        meta = await self._market(market)
        mark_price = await self.get_mark_price(market)
        step = Decimal(1).scaleb(-meta.sz_decimals)
        steps = (MIN_ORDER_NOTIONAL_USD / mark_price / step).to_integral_value(
            rounding=ROUND_CEILING
        )
        minimum = steps * step
        if minimum <= 0:
            raise ValueError(f"Hyperliquid {market} 最小下单量计算结果无效")
        return minimum

    async def _prepare_amount(self, market: str, amount: Decimal) -> Decimal:
        """取整并校验正数量与动态最小下单量。"""
        rounded = await self.round_amount(market, amount)
        if rounded <= 0:
            raise ValueError("Hyperliquid 下单数量取整后必须为正")
        minimum = await self.get_min_order_size(market)
        if rounded < minimum:
            raise ValueError(
                f"Hyperliquid 下单数量 {rounded} 低于当前最小下单量 {minimum}"
            )
        return rounded

    def _order_options(self, *, reduce_only: bool) -> dict[str, Any]:
        """仅在启用时加入 builder，默认调用不得产生 builder 字段。"""
        options: dict[str, Any] = {"reduce_only": reduce_only}
        if self._builder is not None:
            # SDK 会原地把地址转成小写，每次传副本以保持客户端配置不可变。
            options["builder"] = dict(self._builder)
        return options

    @staticmethod
    def _order_result(raw: Any) -> HyperliquidOrderResult:
        """解析下单响应，任何业务拒绝都转为明确异常。"""
        if not isinstance(raw, dict):
            raise RuntimeError("Hyperliquid 下单失败：SDK 响应不是对象")
        if raw.get("status") != "ok":
            raise RuntimeError(f"Hyperliquid 下单失败：{raw.get('response') or raw!r}")
        response = raw.get("response")
        data = response.get("data") if isinstance(response, dict) else None
        statuses = data.get("statuses") if isinstance(data, dict) else None
        if not isinstance(statuses, list) or len(statuses) != 1:
            raise RuntimeError("Hyperliquid 下单失败：响应缺少单笔 statuses")
        status = statuses[0]
        if not isinstance(status, dict):
            raise RuntimeError(f"Hyperliquid 下单失败：{status!r}")
        if status.get("error") is not None:
            raise RuntimeError(f"Hyperliquid 下单失败：{status['error']}")
        for key, normalized in (("resting", "OPEN"), ("filled", "FILLED")):
            detail = status.get(key)
            if isinstance(detail, dict) and detail.get("oid") is not None:
                return HyperliquidOrderResult(
                    id=int(detail["oid"]),
                    status=normalized,
                    raw=raw,
                )
        raise RuntimeError(f"Hyperliquid 下单失败：未知状态 {status!r}")

    async def place_limit_order(
        self,
        market: str,
        side: Side,
        amount: Decimal,
        price: Decimal,
        *,
        post_only: bool = True,
        reduce_only: bool = False,
    ) -> HyperliquidOrderResult:
        """挂限价单；``post_only=True`` 映射为 Hyperliquid 的 ALO。"""
        exchange = self._require_trading()
        coin = (await self._market(market)).coin
        rounded_amount = await self._prepare_amount(market, amount)
        rounded_price = await self.round_price(market, price)
        tif = "Alo" if post_only else "Gtc"
        raw = await asyncio.to_thread(
            exchange.order,
            coin,
            side is Side.BUY,
            float(rounded_amount),
            float(rounded_price),
            {"limit": {"tif": tif}},
            **self._order_options(reduce_only=reduce_only),
        )
        return self._order_result(raw)

    async def market_order(
        self,
        market: str,
        side: Side,
        amount: Decimal,
        *,
        reduce_only: bool = False,
    ) -> HyperliquidOrderResult:
        """用带方向性滑点保护的 IOC 实现市价单，并透传 ``reduce_only``。"""
        exchange = self._require_trading()
        coin = (await self._market(market)).coin
        rounded_amount = await self._prepare_amount(market, amount)
        book = await self.get_market_price(market)
        reference = book.ask if side is Side.BUY else book.bid
        multiplier = (
            Decimal(1) + self._market_order_slippage
            if side is Side.BUY
            else Decimal(1) - self._market_order_slippage
        )
        limit_price = await self.round_price(market, reference * multiplier)
        raw = await asyncio.to_thread(
            exchange.order,
            coin,
            side is Side.BUY,
            float(rounded_amount),
            float(limit_price),
            {"limit": {"tif": "Ioc"}},
            **self._order_options(reduce_only=reduce_only),
        )
        return self._order_result(raw)

    @staticmethod
    def _terminal_cancel_message(value: Any) -> bool:
        """识别 SDK 对成交/已撤订单返回的幂等撤单消息。"""
        message = str(value or "").lower()
        return any(
            marker in message
            for marker in (
                "already canceled",
                "already cancelled",
                "or filled",
                "never placed",
            )
        )

    @classmethod
    def _validate_cancel_result(cls, raw: Any) -> None:
        """验证撤单结果，只吞掉明确的终态竞态。"""
        if not isinstance(raw, dict):
            raise RuntimeError("Hyperliquid 撤单失败：SDK 响应不是对象")
        if raw.get("status") != "ok":
            message = raw.get("response") or raw
            if cls._terminal_cancel_message(message):
                return
            raise RuntimeError(f"Hyperliquid 撤单失败：{message!r}")
        response = raw.get("response")
        data = response.get("data") if isinstance(response, dict) else None
        statuses = data.get("statuses") if isinstance(data, dict) else None
        if not isinstance(statuses, list) or len(statuses) != 1:
            raise RuntimeError("Hyperliquid 撤单失败：响应缺少单笔 statuses")
        status = statuses[0]
        if status == "success" or cls._terminal_cancel_message(status):
            return
        raise RuntimeError(f"Hyperliquid 撤单失败：{status!r}")

    async def cancel_order(self, market: str, order_id) -> None:
        """幂等撤单；已成交或已撤订单不再发写请求，也不抛异常。"""
        exchange = self._require_trading()
        target = int(order_id)
        orders = await self.get_open_orders(market)
        order = next((item for item in orders if item.id == target), None)
        if order is None:
            logger.warning("Hyperliquid 撤单跳过：oid=%s 已不在活动订单中", target)
            return
        raw = await asyncio.to_thread(exchange.cancel, order.market, target)
        self._validate_cancel_result(raw)

    async def close(self) -> None:
        """关闭 SDK 内部 ``requests.Session``；只读未连接实例可重复关闭。"""
        sessions: list[Any] = []
        for owner in (self._info, self._exchange, getattr(self._exchange, "info", None)):
            session = getattr(owner, "session", None)
            if session is not None and all(id(session) != id(item) for item in sessions):
                sessions.append(session)
        for session in sessions:
            close = getattr(session, "close", None)
            if callable(close):
                await asyncio.to_thread(close)
