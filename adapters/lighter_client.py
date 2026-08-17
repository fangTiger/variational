"""Robinhood Chain 上 Lighter 的适配器，默认只读。

默认构造仍只读取人工维护的 primary 腿，不持有密钥，也不允许机器人下单；
只有显式启用交易并提供 API 私钥的独立实例才能调用交易方法。
任何 HTTP、状态码或响应解析失败都会向上抛出，避免把读取故障误判为空仓。
"""

from __future__ import annotations

import time
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx

from adapters.base import ExchangeAdapter, MarketPrice, Position, Side
from adapters.lighter_scale import to_base_amount, to_price
from adapters.order_ref import ClientOrderIndexAllocator, OrderRef

BASE_URL = "https://api.rh.lighter.xyz"
DEFAULT_TIMEOUT = 10.0
DEFAULT_MARKET_ORDER_SLIPPAGE = Decimal("0.01")


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

    @staticmethod
    def _extract_market_details(data: dict) -> list[dict[str, Any]]:
        """提取订单簿详情列表，响应结构异常时抛出。"""
        for key in ("order_book_details", "orderBookDetails"):
            items = data.get(key)
            if isinstance(items, list):
                return LighterClient._validate_dict_items(items, context=key)
        raise ValueError("Lighter 市场详情响应缺少 order_book_details 数组")

    async def get_market_price(self, market: str) -> MarketPrice:
        """用订单簿详情中的标记价构造统一行情。"""
        data = await self._get_json("/api/v1/orderBookDetails")
        self._raise_api_error(data, context="市场详情查询")
        target = market.upper()
        for detail in self._extract_market_details(data):
            if str(detail.get("symbol", "")).upper() != target:
                continue
            mark_price = Decimal(str(detail["mark_price"]))
            if mark_price <= 0:
                raise ValueError(f"Lighter {market} mark_price 必须为正")
            return MarketPrice(market=market, bid=mark_price, ask=mark_price)
        raise KeyError(f"Lighter 市场详情中没有标的 {market}")

    async def get_liquidation_info(self, market: str) -> tuple[Decimal, Decimal] | None:
        """返回标记价与清算价；无仓或清算价为零时返回 None。"""
        position = await self.get_position(market)
        if position.is_flat or not isinstance(position.raw, dict):
            return None
        liquidation_price = Decimal(str(position.raw.get("liquidation_price", "0") or "0"))
        if liquidation_price <= 0:
            return None
        market_price = await self.get_market_price(market)
        return market_price.mid, liquidation_price

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
            }
            self._market_meta[symbol] = meta
            return meta
        raise KeyError(f"Lighter 没有标的 {market}")

    def _require_signer(self):
        """懒加载锁定版本的 SDK 签名器。"""
        if self._signer is None:
            if not self._api_private_key:
                raise RuntimeError("Lighter 鉴权操作需要 api_private_key")
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
        signer = self._require_signer()
        token, err = signer.create_auth_token_with_expiry(
            api_key_index=self._api_key_index
        )
        self._raise_tx_error(err, context="生成鉴权 token")
        if not token:
            raise RuntimeError("Lighter 生成鉴权 token 失败：返回空 token")
        return {"Authorization": str(token)}

    async def get_open_orders(self, market: str) -> list[dict[str, Any]]:
        """查询指定市场的活动订单；该接口即使只读也要求 SDK 鉴权。"""
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
        return self._validate_dict_items(orders, context="orders")

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
        mark_price = (await self.get_market_price(market)).mid
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
        """挂限价单并返回客户端订单引用。

        单笔撤单能力在第二期补齐；当前不解析交易所 order_index，
        只支持按市场整体撤单，因此返回的 OrderRef.id 固定为 None。
        """
        self._require_trading()
        meta = await self._load_market_meta(market)
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
        return OrderRef(id=None, client_order_index=client_order_index)

    async def cancel_order(self, market: str, order_id) -> None:
        """单笔撤单第二期实现；当前一期只支持按市场整体撤单。"""
        self._require_trading()
        del market, order_id
        raise NotImplementedError("单笔撤单能力将在第二期补齐，当前只支持整体撤单")

    async def cancel_all_orders(self, market: str):
        """撤销指定市场的全部活动订单，不依赖单笔订单号。"""
        self._require_trading()
        meta = await self._load_market_meta(market)
        signer = self._require_signer()
        _tx, response, err = await signer.cancel_all_orders(
            time_in_force=signer.ORDER_TIME_IN_FORCE_IMMEDIATE_OR_CANCEL,
            timestamp_ms=int(time.time() * 1000),
            cancel_all_market_index=meta["market_id"],
        )
        self._validate_tx_response(response, err, context="整体撤单")
        return response

    async def close_position(self, market: str):
        """通过基类通用逻辑以 reduce-only 市价单平掉当前仓位。"""
        self._require_trading()
        return await super().close_position(market)

    async def close(self) -> None:
        """释放 HTTP 连接池。"""
        await self._http.aclose()
