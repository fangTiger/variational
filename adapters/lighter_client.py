"""Robinhood Chain 上 Lighter 的适配器，默认只读。

默认构造仍只读取人工维护的 primary 腿，不持有密钥，也不允许机器人下单；
只有显式启用交易并提供 API 私钥的独立实例才能调用交易方法。
任何 HTTP、状态码或响应解析失败都会向上抛出，避免把读取故障误判为空仓。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx

from adapters.base import ExchangeAdapter, MarketPrice, Position, PositionPnl, Side
from adapters.lighter_order import LighterOrder, filter_grid_orders
from adapters.lighter_scale import from_base_amount, to_base_amount, to_price
from adapters.order_ref import ClientOrderIndexAllocator, OrderRef
from infra.logger import get_logger
from infra.runtime import ensure_ssl_cert

logger = get_logger("lighter_client")

BASE_URL = "https://api.rh.lighter.xyz"
DEFAULT_TIMEOUT = 10.0
DEFAULT_MARKET_ORDER_SLIPPAGE = Decimal("0.01")


@dataclass(frozen=True)
class LighterBalance:
    """供通用引擎读取的 Lighter 权益视图。"""

    equity: Decimal


class LighterClient(ExchangeAdapter):
    """Lighter Robinhood Chain 客户端，默认只读。"""

    name = "lighter-rh"

    def __init__(
        self,
        l1_address: str,
        base_url: str = BASE_URL,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        trading_enabled: bool = False,
        api_private_key: str | None = None,
        api_key_index: int = 255,
        account_index: int | None = None,
        client_order_index_path: str | None = None,
    ) -> None:
        """默认只读；只有显式启用并提供密钥才允许交易。"""
        if trading_enabled and not api_private_key:
            raise ValueError("trading_enabled=True 时必须提供 api_private_key")

        self.l1_address = l1_address
        self.base_url = base_url.rstrip("/")
        self.account_index: int | None = account_index
        self.trading_enabled = trading_enabled
        self._api_private_key = api_private_key
        self._api_key_index = api_key_index
        self._signer = None
        self._market_meta: dict[str, dict[str, Any]] = {}
        self._coi = ClientOrderIndexAllocator(
            Path(client_order_index_path or "data/lighter_client_order_index.json")
        )
        self._http = httpx.AsyncClient(base_url=self.base_url, timeout=timeout)

    @property
    def supports_trading(self) -> bool:
        """与交易开关同源，供对冲引擎判断该腿是否允许自动交易。"""
        return self.trading_enabled

    def _require_trading(self) -> None:
        """所有会改变挂单或仓位的操作都必须先过这道闸。"""
        if not self.trading_enabled:
            raise PermissionError(
                "该 LighterClient 实例是只读的，禁止下单撤单；"
                "需以 trading_enabled=True 显式构造交易实例"
            )

    async def _get_json(
        self,
        path: str,
        *,
        params: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict:
        """发送 GET 并返回 JSON 对象；任何失败都直接抛出。"""
        response = await self._http.get(path, params=params, headers=headers)
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            raise ValueError(f"Lighter {path} 响应不是 JSON 对象")
        return data

    async def _get_json_decimal(
        self,
        path: str,
        *,
        params: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict:
        """为只读统计请求解析 Decimal，避免金额先经过二进制浮点数。"""
        response = await self._http.get(path, params=params, headers=headers)
        response.raise_for_status()
        data = json.loads(
            response.text,
            parse_float=Decimal,
            parse_int=int,
        )
        if not isinstance(data, dict):
            raise ValueError(f"Lighter {path} 响应不是 JSON 对象")
        return data

    @staticmethod
    def _raise_api_error(data: dict, *, context: str) -> None:
        """把业务错误码转换为明确异常。"""
        code = data.get("code")
        if code in (0, 200, None):
            return
        message = data.get("message") or "未知错误"
        if code == 21100:
            raise RuntimeError(f"Lighter 地址未开户：{message}")
        raise RuntimeError(f"Lighter {context}失败（code={code}）：{message}")

    async def connect(self) -> None:
        """按 L1 地址解析并缓存账户索引。"""
        data = await self._get_json(
            "/api/v1/accountsByL1Address",
            params={"l1_address": self.l1_address},
        )
        self._raise_api_error(data, context="账户反查")

        candidates: list[dict[str, Any]] = []
        for key in ("sub_accounts", "accounts"):
            accounts = data.get(key)
            if isinstance(accounts, list):
                candidates.extend(item for item in accounts if isinstance(item, dict))
        account = data.get("account")
        if isinstance(account, dict):
            candidates.append(account)
        candidates.append(data)

        for item in candidates:
            value = item.get("account_index", item.get("index"))
            if value is not None:
                self.account_index = int(value)
                return
        raise RuntimeError("Lighter 账户反查响应缺少 account_index")

    def _require_account_index(self) -> int:
        """返回已缓存索引；未连接时拒绝继续。"""
        if self.account_index is None:
            raise RuntimeError("请先 await connect() 解析 Lighter 账户索引")
        return self.account_index

    @staticmethod
    def _validate_dict_items(items: list, *, context: str) -> list[dict[str, Any]]:
        """拒绝列表中的畸形元素，避免把解析失败误判为空结果。"""
        if any(not isinstance(item, dict) for item in items):
            raise ValueError(f"Lighter {context}数组包含非对象元素")
        return items

    @staticmethod
    def _extract_positions(data: dict) -> list[dict[str, Any]]:
        """从账户详情响应中提取 positions，结构异常时拒绝伪装为空仓。"""
        direct = data.get("positions")
        if isinstance(direct, list):
            return LighterClient._validate_dict_items(direct, context="positions")

        account = data.get("account")
        if isinstance(account, dict) and isinstance(account.get("positions"), list):
            return LighterClient._validate_dict_items(
                account["positions"], context="positions"
            )

        accounts = data.get("accounts")
        if isinstance(accounts, list) and accounts:
            first = accounts[0]
            if isinstance(first, dict) and isinstance(first.get("positions"), list):
                return LighterClient._validate_dict_items(
                    first["positions"], context="positions"
                )

        raise ValueError("Lighter 账户详情响应缺少 positions 数组")

    async def get_position(self, market: str) -> Position:
        """读取并归一化持仓；sign=1 为多头，sign=-1 为空头。"""
        account_index = self._require_account_index()
        data = await self._get_json(
            "/api/v1/account",
            params={"by": "index", "value": str(account_index)},
        )
        self._raise_api_error(data, context="账户详情查询")

        target = market.upper()
        for raw in self._extract_positions(data):
            if str(raw.get("symbol", "")).upper() != target:
                continue
            size = Decimal(str(raw["position"]))
            sign = Decimal(str(raw["sign"]))
            if size < 0:
                raise ValueError(f"Lighter {market} position 不得为负")
            if size != 0 and sign not in (Decimal(-1), Decimal(1)):
                raise ValueError(f"Lighter {market} 非零仓位 sign 必须为 1 或 -1")
            return Position(market=market, signed_size=sign * size, raw=raw)
        return Position(market=market, signed_size=Decimal(0))

    async def get_position_pnl(self, market: str) -> PositionPnl | None:
        """从账户持仓读取未实现盈亏、入场价与仓位价值。"""
        account_index = self._require_account_index()
        data = await self._get_json(
            "/api/v1/account",
            params={"by": "index", "value": str(account_index)},
        )
        self._raise_api_error(data, context="账户详情查询")

        target = market.upper()
        for raw in self._extract_positions(data):
            if str(raw.get("symbol", "")).upper() != target:
                continue

            def optional_decimal(field: str) -> Decimal | None:
                value = raw.get(field)
                return Decimal(str(value)) if value not in (None, "") else None

            return PositionPnl(
                unrealized_pnl=optional_decimal("unrealized_pnl"),
                entry_price=optional_decimal("avg_entry_price"),
                position_value=optional_decimal("position_value"),
            )
        return None

    async def get_collateral(self) -> Decimal:
        """账户抵押品总额。仅供监控汇总，不参与任何交易决策。"""
        account_index = self._require_account_index()
        data = await self._get_json(
            "/api/v1/account",
            params={"by": "index", "value": str(account_index)},
        )
        self._raise_api_error(data, context="账户详情查询")
        accounts = data.get("accounts")
        if not isinstance(accounts, list) or not accounts:
            raise ValueError("Lighter 账户详情响应缺少 accounts")
        return Decimal(str(accounts[0]["collateral"]))

    async def get_balance(self) -> LighterBalance:
        """返回包含未实现盈亏的账户权益视图。

        权益读取 ``/api/v1/account`` 的 ``total_asset_value``。按 Lighter
        官方口径，Total Account Value = Collateral + Unrealized PnL，
        因而该值包含全部未平仓仓位的未实现盈亏，与 ``get_collateral()``
        返回的抵押品不是同一口径。查询、响应结构或非正权益异常会原样抛出，
        绝不把读取失败伪造成零权益。
        """
        account_index = self._require_account_index()
        data = await self._get_json(
            "/api/v1/account",
            params={"by": "index", "value": str(account_index)},
        )
        self._raise_api_error(data, context="账户详情查询")
        accounts = data.get("accounts")
        if (
            not isinstance(accounts, list)
            or not accounts
            or not isinstance(accounts[0], dict)
        ):
            raise ValueError("Lighter 账户详情响应缺少 accounts")
        raw_equity = accounts[0].get("total_asset_value")
        if raw_equity is None:
            raise ValueError("Lighter 账户详情响应缺少 total_asset_value")
        equity = Decimal(str(raw_equity))
        if not equity.is_finite() or equity <= 0:
            raise ValueError(f"Lighter 账户权益必须为正数：{raw_equity!r}")
        return LighterBalance(equity=equity)

    @staticmethod
    def _extract_market_details(data: dict) -> list[dict[str, Any]]:
        """提取订单簿详情列表，响应结构异常时抛出。"""
        for key in ("order_book_details", "orderBookDetails"):
            items = data.get(key)
            if isinstance(items, list):
                return LighterClient._validate_dict_items(items, context=key)
        raise ValueError("Lighter 市场详情响应缺少 order_book_details 数组")

    async def get_market_price(self, market: str) -> MarketPrice | None:
        """读取真实买一卖一；盘口不可用时返回 None 供引擎降级放行。"""
        try:
            meta = await self._load_market_meta(market)
            data = await self._get_json(
                "/api/v1/orderBookOrders",
                params={"market_id": str(meta["market_id"]), "limit": "1"},
            )
            self._raise_api_error(data, context="盘口查询")
            asks = data.get("asks")
            bids = data.get("bids")
            if not isinstance(asks, list) or not asks:
                return None
            if not isinstance(bids, list) or not bids:
                return None
            if not isinstance(asks[0], dict) or not isinstance(bids[0], dict):
                return None
            ask = Decimal(str(asks[0]["price"]))
            bid = Decimal(str(bids[0]["price"]))
            if bid <= 0 or ask <= 0 or bid >= ask:
                return None
            return MarketPrice(market=market, bid=bid, ask=ask)
        except Exception:  # noqa: BLE001 盘口保护必须失败开放，由引擎统一告警
            return None

    async def get_liquidation_info(self, market: str) -> tuple[Decimal, Decimal] | None:
        """返回标记价与清算价；无仓或清算价为零时返回 None。"""
        position = await self.get_position(market)
        if position.is_flat or not isinstance(position.raw, dict):
            return None
        liquidation_price = Decimal(str(position.raw.get("liquidation_price", "0") or "0"))
        if liquidation_price <= 0:
            return None
        mark_price = await self.get_mark_price(market)
        return mark_price, liquidation_price

    async def _load_market_meta(self, market: str) -> dict[str, Any]:
        """读取并缓存市场精度；交易参数绝不依靠猜测。"""
        symbol = market.upper()
        if symbol in self._market_meta:
            return self._market_meta[symbol]

        data = await self._get_json("/api/v1/orderBooks")
        self._raise_api_error(data, context="市场列表查询")
        books = data.get("order_books")
        if not isinstance(books, list):
            raise ValueError("Lighter 市场列表响应缺少 order_books 数组")

        for book in self._validate_dict_items(books, context="order_books"):
            if str(book.get("symbol", "")).upper() != symbol:
                continue
            size_decimals = int(book["supported_size_decimals"])
            min_base_amount = Decimal(str(book["min_base_amount"]))
            meta = {
                "market_id": int(book["market_id"]),
                "size_decimals": size_decimals,
                "price_decimals": int(book["supported_price_decimals"]),
                "min_base_units": to_base_amount(
                    min_base_amount,
                    size_decimals,
                ),
                # 最小名义额，与最小数量是两道独立门槛
                "min_quote_amount": Decimal(str(book["min_quote_amount"])),
            }
            self._market_meta[symbol] = meta
            return meta
        raise KeyError(f"Lighter 没有标的 {market}")

    async def get_min_order_size(self, market: str) -> Decimal:
        """从已缓存市场元数据把最小整数单位换算为基础资产数量。"""
        meta = await self._load_market_meta(market)
        return from_base_amount(
            int(meta["min_base_units"]),
            int(meta["size_decimals"]),
        )

    @staticmethod
    def _require_min_quote(
        meta: dict[str, Any],
        price: Decimal,
        amount: Decimal,
    ) -> None:
        """校验名义额门槛。

        Lighter 有两道独立门槛：最小数量 `min_base_amount` 与最小名义额
        `min_quote_amount`。只满足前者仍会被交易所以
        `code=21706 invalid order base or quote amount` 拒绝，而该错误码
        不指明是哪一道，配置切得过小时网格会"看起来在跑"却每笔都被拒。
        """
        minimum = meta.get("min_quote_amount")
        if minimum is None:
            return
        notional = Decimal(str(price)) * Decimal(str(amount))
        if notional < Decimal(str(minimum)):
            raise ValueError(
                f"Lighter 订单名义额 {notional} 低于最小名义额 {minimum}"
                f"（价格 {price} × 数量 {amount}）"
            )

    def _require_signer(self):
        """懒加载锁定版本的 SDK 签名器。

        SDK 内部用 aiohttp 提交交易，会读系统 CA 库；macOS 上该库常为空，
        导致写操作 SSL 校验失败而读操作（httpx 自带 certifi）照常。
        入口文件各自调用 ensure_ssl_cert() 不可靠，故在此兜底。
        """
        if self._signer is None:
            if not self._api_private_key:
                raise RuntimeError("Lighter 鉴权操作需要 api_private_key")
            ensure_ssl_cert()
            import lighter

            self._signer = lighter.SignerClient(
                url=self.base_url,
                account_index=self._require_account_index(),
                api_private_keys={self._api_key_index: self._api_private_key},
            )
        return self._signer

    @staticmethod
    def _raise_tx_error(err, *, context: str) -> None:
        """SDK 把错误放在返回元组末项，不会自动抛异常。"""
        if err is not None:
            raise RuntimeError(f"Lighter {context}失败：{err}")

    @classmethod
    def _validate_tx_response(cls, response, err, *, context: str) -> None:
        """同时验证签名错误与交易响应业务码。"""
        cls._raise_tx_error(err, context=context)
        if response is None:
            raise RuntimeError(f"Lighter {context}失败：SDK 未返回交易响应")
        code = getattr(response, "code", None)
        if code != 200:
            message = getattr(response, "message", None) or "未知错误"
            raise RuntimeError(
                f"Lighter {context}失败（code={code}）：{message}"
            )

    def _auth_headers(self) -> dict[str, str]:
        """为必须鉴权的只读接口生成短期 Authorization 头。"""
        import lighter

        signer = self._require_signer()
        token, err = signer.create_auth_token_with_expiry(
            lighter.SignerClient.DEFAULT_10_MIN_AUTH_EXPIRY,
            api_key_index=self._api_key_index
        )
        self._raise_tx_error(err, context="生成鉴权 token")
        if not token:
            raise RuntimeError("Lighter 生成鉴权 token 失败：返回空 token")
        return {"Authorization": str(token)}

    async def get_open_orders(self, market: str) -> list[LighterOrder]:
        """查询指定市场的活动订单；该接口即使只读也要求 SDK 鉴权。

        返回属性视图而非裸 dict：引擎与 `filter_grid_orders` 全部走 getattr，
        裸 dict 会让 `reduce_only` 恒取默认值 False，把交易所端保护单
        当成普通网格单撤掉。
        """
        meta = await self._load_market_meta(market)
        data = await self._get_json(
            "/api/v1/accountActiveOrders",
            params={
                "account_index": str(self._require_account_index()),
                "market_id": str(meta["market_id"]),
            },
            headers=self._auth_headers(),
        )
        self._raise_api_error(data, context="活动订单查询")
        orders = data.get("orders")
        if not isinstance(orders, list):
            raise ValueError("Lighter 活动订单响应缺少 orders 数组")
        return LighterOrder.from_api_list(
            self._validate_dict_items(orders, context="orders"),
            context="活动订单",
        )

    async def get_orders_history(
        self,
        market: str,
        limit: int = 100,
        *,
        order_type: str | None = None,
        sort: str | None = None,
    ) -> list[LighterOrder]:
        """查询历史（非活动）订单，供引擎判定成交 vs 过期/被撤。

        Lighter 的 `accountInactiveOrders` 不支持按类型过滤或指定排序，
        接口保留 `order_type` / `sort` 仅为与统一适配器契约兼容；
        过滤由调用方自行完成。
        """
        del order_type, sort
        meta = await self._load_market_meta(market)
        data = await self._get_json(
            "/api/v1/accountInactiveOrders",
            params={
                "account_index": str(self._require_account_index()),
                "market_id": str(meta["market_id"]),
                "limit": str(limit),
            },
            headers=self._auth_headers(),
        )
        self._raise_api_error(data, context="历史订单查询")
        orders = data.get("orders")
        if not isinstance(orders, list):
            raise ValueError("Lighter 历史订单响应缺少 orders 数组")
        return LighterOrder.from_api_list(
            self._validate_dict_items(orders, context="orders"),
            context="历史订单",
        )

    async def get_order_by_id(
        self,
        market: str,
        order_id,
    ) -> LighterOrder | None:
        """按引擎侧 ``client_order_index`` 查询活动单或近期历史单。

        Lighter 没有在本适配器中暴露稳定的单笔查询端点，因此依次复用活动单
        与历史单接口。maker 超时后的撤单竞态通常只涉及刚创建的订单，近期
        历史窗口足以覆盖；两个来源都未命中属于正常结果，返回 ``None``。
        """
        target = int(order_id)
        for order in await self.get_open_orders(market):
            if order.id == target:
                return order
        for order in await self.get_orders_history(market, limit=100):
            if order.id == target:
                return order
        return None

    async def market_order(
        self,
        market: str,
        side: Side,
        amount: Decimal,
        *,
        reduce_only: bool = False,
    ):
        """提交 IOC 市价单，允许相对标记价最多 1% 的方向性滑点。"""
        self._require_trading()
        meta = await self._load_market_meta(market)
        signer = self._require_signer()
        client_order_index = self._coi.next()
        mark_price = await self.get_mark_price(market)
        self._require_min_quote(meta, mark_price, amount)
        price_multiplier = (
            Decimal(1) - DEFAULT_MARKET_ORDER_SLIPPAGE
            if side is Side.SELL
            else Decimal(1) + DEFAULT_MARKET_ORDER_SLIPPAGE
        )
        _tx, response, err = await signer.create_market_order(
            market_index=meta["market_id"],
            client_order_index=client_order_index,
            base_amount=to_base_amount(
                amount,
                meta["size_decimals"],
                min_base_units=meta["min_base_units"],
            ),
            avg_execution_price=to_price(
                mark_price * price_multiplier,
                meta["price_decimals"],
            ),
            is_ask=(side is Side.SELL),
            reduce_only=reduce_only,
        )
        self._validate_tx_response(response, err, context="下市价单")
        return response

    async def place_limit_order(
        self,
        market: str,
        side: Side,
        amount: Decimal,
        price: Decimal,
        *,
        post_only: bool = True,
        reduce_only: bool = False,
    ):
        """挂限价单并返回订单引用。

        `OrderRef.id` 是 `client_order_index`，不是交易所的 `order_index`。
        Lighter 的下单响应只含 tx_hash，`order_index` 要事后查询才有；
        而引擎在返回值缺 id 时会判定挂单失败并原格重挂
        （`grid_engine.py:1770`），留下挂在交易所却不被跟踪的孤儿单。
        `client_order_index` 由本地分配、下单当场即有，故用作统一身份，
        由 `cancel_order` 在撤单时转换回 `order_index`。
        """
        self._require_trading()
        meta = await self._load_market_meta(market)
        self._require_min_quote(meta, price, amount)
        signer = self._require_signer()
        client_order_index = self._coi.next()
        _tx, _response, err = await signer.create_order(
            market_index=meta["market_id"],
            client_order_index=client_order_index,
            base_amount=to_base_amount(
                amount,
                meta["size_decimals"],
                min_base_units=meta["min_base_units"],
            ),
            price=to_price(price, meta["price_decimals"]),
            is_ask=(side is Side.SELL),
            order_type=signer.ORDER_TYPE_LIMIT,
            time_in_force=(
                signer.ORDER_TIME_IN_FORCE_POST_ONLY
                if post_only
                else signer.ORDER_TIME_IN_FORCE_GOOD_TILL_TIME
            ),
            reduce_only=reduce_only,
        )
        self._validate_tx_response(_response, err, context="挂限价单")
        return OrderRef(
            id=client_order_index,
            client_order_index=client_order_index,
        )

    async def cancel_order(self, market: str, order_id) -> None:
        """撤单。`order_id` 是 `client_order_index`（引擎侧身份）。

        签名需要交易所侧的 `order_index`，两者不是一回事，因此先查活动
        订单做一次转换。若该单已不在挂单簿上（成交或已撤），视为成功：
        引擎撤单失败时会保留记录下轮重试（`grid_engine.py:1809`），
        在这里抛错会让已成交的订单被无限重试撤单，形成活锁。
        """
        self._require_trading()
        meta = await self._load_market_meta(market)
        target = int(order_id)
        for order in await self.get_open_orders(market):
            if order.id == target:
                await self._cancel_by_order_index(meta["market_id"], order.order_index)
                return
        logger.warning(
            "Lighter 撤单跳过：client_order_index=%s 已不在挂单簿上", target
        )

    async def _cancel_by_order_index(self, market_id: int, order_index: int) -> None:
        """按交易所 order_index 撤单。"""
        signer = self._require_signer()
        _tx, response, err = await signer.cancel_order(
            market_index=market_id,
            order_index=int(order_index),
        )
        self._validate_tx_response(response, err, context="撤单")

    async def cancel_grid_orders(self, market: str) -> int:
        """逐单撤掉普通网格单，保留 reduce-only 与条件单。

        直接用已查到的 `order_index`，不再走 `cancel_order` 的二次查询。
        """
        self._require_trading()
        meta = await self._load_market_meta(market)
        orders = filter_grid_orders(await self.get_open_orders(market))
        for order in orders:
            await self._cancel_by_order_index(meta["market_id"], order.order_index)
        return len(orders)

    async def cancel_all_orders(self, market: str):
        """撤销指定市场的全部活动订单，不依赖单笔订单号。

        IOC 模式下 CancelAllTime 必须为 nil，传非零时间戳会被交易所以
        「CancelAllTime should be nil」拒绝；timestamp_ms 只服务于
        SCHEDULED 模式的定时撤单。
        """
        self._require_trading()
        meta = await self._load_market_meta(market)
        signer = self._require_signer()
        _tx, response, err = await signer.cancel_all_orders(
            time_in_force=signer.ORDER_TIME_IN_FORCE_IMMEDIATE_OR_CANCEL,
            timestamp_ms=0,
            cancel_all_market_index=meta["market_id"],
        )
        self._validate_tx_response(response, err, context="整体撤单")
        return response

    async def get_mark_price(self, market: str) -> Decimal:
        """标记价。

        标记价继续读取订单簿详情，不能用真实盘口中值替代，否则清算距离、
        硬止损和市价单滑点基准都会发生无关变化。
        """
        data = await self._get_json("/api/v1/orderBookDetails")
        self._raise_api_error(data, context="市场详情查询")
        target = market.upper()
        for detail in self._extract_market_details(data):
            if str(detail.get("symbol", "")).upper() != target:
                continue
            mark_price = Decimal(str(detail["mark_price"]))
            if mark_price <= 0:
                raise ValueError(f"Lighter {market} mark_price 必须为正")
            return mark_price
        raise KeyError(f"Lighter 市场详情中没有标的 {market}")

    async def round_price(self, market: str, price: Decimal) -> Decimal:
        """按市场价格精度对齐。

        引擎在翻单重试对账时用它把目标价与交易所返回价对齐比较
        （`grid_engine.py:1131`）。若沿用基类的原样返回，比较必然失配，
        引擎会误判自己的单不存在而重复挂单。
        """
        meta = await self._load_market_meta(market)
        return self._quantize(price, meta["price_decimals"])

    async def round_amount(self, market: str, amount: Decimal) -> Decimal:
        """按市场数量步长对齐，理由同 `round_price`。"""
        meta = await self._load_market_meta(market)
        return self._quantize(amount, meta["size_decimals"])

    async def get_price_tick_size(self, market: str) -> Decimal:
        """返回价格最小变动单位。

        引擎用 tick/2 作为整仓止损触发价的比对容差
        （`grid_engine.py:549`）；返回 None 会让容差退化，
        止损单被反复判定为"与交易所不一致"而重挂。
        """
        meta = await self._load_market_meta(market)
        return Decimal(1).scaleb(-int(meta["price_decimals"]))

    @staticmethod
    def _quantize(value: Decimal, decimals: int) -> Decimal:
        """按小数位数量化，采用四舍五入。"""
        return Decimal(str(value)).quantize(Decimal(1).scaleb(-int(decimals)))

    async def place_position_stop_loss(
        self,
        market: str,
        signed_size: Decimal,
        trigger_price: Decimal,
    ):
        """为当前整仓挂 reduce-only 止损，多仓平多、空仓平空。

        **与 Extended 的关键差异：先撤旧单再挂新单。**
        Extended 的整仓 TPSL 是单例，重挂即替换；Lighter 的 `create_sl_order`
        每次新建一张。而引擎在持仓变化时就会重挂（`grid_engine.py:519`），
        网格每笔成交都改变持仓——不撤旧单会累积出成百上千张陈旧止损单，
        触发时一起打出去，把平仓变成反向开仓。

        止损单是 IOC，限价须朝平仓方向留出滑点空间，否则永远吃不到，
        止损形同虚设。
        """
        signed_size = Decimal(str(signed_size))
        trigger_price = Decimal(str(trigger_price))
        if signed_size == 0:
            raise ValueError("空仓不能挂整仓止损")
        if trigger_price <= 0:
            raise ValueError("止损触发价必须大于 0")

        self._require_trading()
        meta = await self._load_market_meta(market)

        # 必须先撤旧单，理由见 docstring
        await self.cancel_tpsl(market)

        is_ask = signed_size > 0  # 多仓靠卖出平掉
        slippage = (
            Decimal(1) - DEFAULT_MARKET_ORDER_SLIPPAGE
            if is_ask
            else Decimal(1) + DEFAULT_MARKET_ORDER_SLIPPAGE
        )
        signer = self._require_signer()
        _tx, response, err = await signer.create_sl_order(
            market_index=meta["market_id"],
            client_order_index=self._coi.next(),
            base_amount=to_base_amount(
                abs(signed_size),
                meta["size_decimals"],
                min_base_units=meta["min_base_units"],
            ),
            trigger_price=to_price(trigger_price, meta["price_decimals"]),
            price=to_price(trigger_price * slippage, meta["price_decimals"]),
            is_ask=is_ask,
            reduce_only=True,
        )
        self._validate_tx_response(response, err, context="挂整仓止损")
        return response

    async def cancel_tpsl(self, market: str) -> None:
        """撤掉该市场的整仓止损，不影响普通网格单。"""
        self._require_trading()
        meta = await self._load_market_meta(market)
        for order in await self.get_open_orders(market):
            if order.is_position_stop_loss:
                await self._cancel_by_order_index(meta["market_id"], order.order_index)

    async def get_position_tpsl(self, market: str) -> LighterOrder | None:
        """查询当前市场挂出的整仓止损；不存在返回 None。

        引擎读返回值的 `.trigger_price` 校验交易所侧止损是否仍然有效
        （`grid_engine.py:537`）。
        """
        for order in await self.get_open_orders(market):
            if order.is_position_stop_loss:
                return order
        return None

    async def close_position(self, market: str):
        """通过基类通用逻辑以 reduce-only 市价单平掉当前仓位。"""
        self._require_trading()
        return await super().close_position(market)

    async def close(self) -> None:
        """释放 HTTP 连接池。"""
        await self._http.aclose()
