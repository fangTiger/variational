"""Robinhood Chain 上 Lighter 的公开只读适配器。

该适配器只读取人工维护的 primary 腿，不持有密钥，也不允许机器人下单。
任何 HTTP、状态码或响应解析失败都会向上抛出，避免把读取故障误判为空仓。
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import httpx

from adapters.base import ExchangeAdapter, MarketPrice, Position, Side

BASE_URL = "https://api.rh.lighter.xyz"
DEFAULT_TIMEOUT = 10.0


class LighterClient(ExchangeAdapter):
    """Lighter Robinhood Chain 公开 API 的只读客户端。"""

    name = "lighter-rh"
    supports_trading = False

    def __init__(
        self,
        l1_address: str,
        base_url: str = BASE_URL,
        *,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self.l1_address = l1_address
        self.base_url = base_url.rstrip("/")
        self.account_index: int | None = None
        self._http = httpx.AsyncClient(base_url=self.base_url, timeout=timeout)

    async def _get_json(self, path: str, *, params: dict[str, str] | None = None) -> dict:
        """发送公开 GET 并返回 JSON 对象；任何失败都直接抛出。"""
        response = await self._http.get(path, params=params)
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
        accounts = data.get("accounts")
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

    async def market_order(
        self,
        market: str,
        side: Side,
        amount: Decimal,
        *,
        reduce_only: bool = False,
    ):
        """始终拒绝下单：该腿由人工操作，机器人只读；这是有意的设计约束。"""
        del market, side, amount, reduce_only
        raise NotImplementedError("Lighter primary 腿由人工操作，机器人只读，禁止自动下单")

    async def close_position(self, market: str):
        """始终拒绝平仓：该腿由人工操作，机器人只读；这是有意的设计约束。"""
        del market
        raise NotImplementedError("Lighter primary 腿由人工操作，机器人只读，禁止自动平仓")

    async def close(self) -> None:
        """释放 HTTP 连接池。"""
        await self._http.aclose()
