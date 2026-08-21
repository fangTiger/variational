"""Hyperliquid 适配器的无网络契约测试。"""

from __future__ import annotations

import asyncio
import importlib
import inspect
from decimal import Decimal

import pytest

from adapters.base import ExchangeAdapter, Side


ACCOUNT_ADDRESS = "0x1111111111111111111111111111111111111111"
AGENT_PRIVATE_KEY = "0x" + "22" * 32


def _meta_and_contexts(mark_price: str = "62500") -> list:
    """返回与 SDK 文档一致的完整永续元数据响应。"""
    return [
        {
            "universe": [
                {
                    "name": "BTC",
                    "szDecimals": 5,
                    "maxLeverage": 50,
                    "onlyIsolated": False,
                },
                {
                    "name": "ETH",
                    "szDecimals": 4,
                    "maxLeverage": 50,
                    "onlyIsolated": False,
                },
            ]
        },
        [
            {
                "dayNtlVlm": "1000000",
                "funding": "0.00001",
                "impactPxs": ["62499", "62501"],
                "markPx": mark_price,
                "midPx": "100",
                "openInterest": "1000",
                "oraclePx": "62498",
                "premium": "0.0001",
                "prevDayPx": "62000",
                "dayBaseVlm": "16",
            },
            {
                "dayNtlVlm": "500000",
                "funding": "0.00002",
                "impactPxs": ["2999", "3001"],
                "markPx": "3000",
                "midPx": "3000",
                "openInterest": "2000",
                "oraclePx": "2998",
                "premium": "0.0002",
                "prevDayPx": "2950",
                "dayBaseVlm": "166",
            },
        ],
    ]


def _book(bid: str = "62490", ask: str = "62510") -> dict:
    """返回与 ``Info.l2_snapshot`` 文档一致的完整盘口响应。"""
    return {
        "coin": "BTC",
        "levels": [
            [{"n": 3, "px": bid, "sz": "1.25"}],
            [{"n": 2, "px": ask, "sz": "0.75"}],
        ],
        "time": 1_775_000_000_000,
    }


def _position(coin: str, size: str, liquidation_price: str | None) -> dict:
    """返回与 ``Info.user_state`` 文档一致的完整持仓元素。"""
    return {
        "position": {
            "coin": coin,
            "entryPx": "60000" if coin == "BTC" else "3100",
            "leverage": {"type": "cross", "value": 5},
            "liquidationPx": liquidation_price,
            "marginUsed": "10",
            "positionValue": "100",
            "returnOnEquity": "0.1",
            "szi": size,
            "unrealizedPnl": "1.5",
        },
        "type": "oneWay",
    }


def _user_state(
    positions: list[dict] | None = None,
    *,
    account_value: str = "125.5",
    withdrawable: str = "103.25",
) -> dict:
    """返回与 SDK 文档一致的完整账户状态响应。"""
    return {
        "assetPositions": positions or [],
        "crossMarginSummary": {
            "accountValue": account_value,
            "totalMarginUsed": "20",
            "totalNtlPos": "200",
            "totalRawUsd": account_value,
        },
        "marginSummary": {
            "accountValue": account_value,
            "totalMarginUsed": "22",
            "totalNtlPos": "220",
            "totalRawUsd": account_value,
        },
        "withdrawable": withdrawable,
    }


def _spot_user_state(
    usdc_total: str = "0",
    *,
    extra_balances: list[dict] | None = None,
) -> dict:
    """返回 ``Info.spot_user_state`` 的真实完整余额元素结构。"""
    return {
        "balances": [
            {
                "coin": "USDC",
                "hold": "0.0",
                "total": usdc_total,
                "entryNtl": "0.0",
            },
            *(extra_balances or []),
        ]
    }


def _open_order(coin: str, oid: int, *, reduce_only: bool = False) -> dict:
    """返回与 ``Info.frontend_open_orders`` 文档一致的完整订单。"""
    return {
        "children": [],
        "cloid": None,
        "coin": coin,
        "isPositionTpsl": False,
        "isTrigger": False,
        "limitPx": "62500" if coin == "BTC" else "3000",
        "oid": oid,
        "orderType": "Limit",
        "origSz": "0.002",
        "reduceOnly": reduce_only,
        "side": "B",
        "sz": "0.002",
        "tif": "Alo",
        "timestamp": 1_775_000_000_000,
        "triggerCondition": "N/A",
        "triggerPx": "0",
    }


def _historical_order(coin: str, oid: int, status: str = "filled") -> dict:
    """返回与 ``Info.historical_orders`` 文档一致的历史订单元素。"""
    order = _open_order(coin, oid)
    order["sz"] = "0"
    return {
        "order": order,
        "status": status,
        "statusTimestamp": 1_775_000_001_000,
    }


def _order_response(oid: int = 101, state: str = "resting") -> dict:
    """返回与 ``Exchange.order`` 一致的单笔成功响应。"""
    detail = {"oid": oid}
    if state == "filled":
        detail.update({"totalSz": "0.001", "avgPx": "62500"})
    return {
        "status": "ok",
        "response": {
            "type": "order",
            "data": {"statuses": [{state: detail}]},
        },
    }


def _order_error_response(message: str) -> dict:
    """返回 SDK 对业务拒绝的真实 statuses 错误结构。"""
    return {
        "status": "ok",
        "response": {
            "type": "order",
            "data": {"statuses": [{"error": message}]},
        },
    }


class FakeInfo:
    """只替代同步 HTTP 边界，方法签名逐项对齐 SDK 0.24.0。"""

    def __init__(
        self,
        *,
        meta_contexts: list | None = None,
        book: dict | None = None,
        state: dict | None = None,
        spot_state: dict | None = None,
        orders: list[dict] | None = None,
        history: list[dict] | None = None,
        query_result: dict | None = None,
    ) -> None:
        self.meta_contexts = meta_contexts or _meta_and_contexts()
        self.book = book or _book()
        self.state = state or _user_state()
        self.spot_state = spot_state or _spot_user_state()
        self.orders = orders or []
        self.history = history or []
        self.query_result = query_result or {"status": "unknownOid"}
        self.user_state_error: Exception | None = None
        self.spot_state_error: Exception | None = None
        self.query_error: Exception | None = None
        self.calls: list[tuple] = []

    def meta_and_asset_ctxs(self):
        self.calls.append(("meta_and_asset_ctxs",))
        return self.meta_contexts

    def l2_snapshot(self, name: str):
        self.calls.append(("l2_snapshot", name))
        return self.book

    def user_state(self, address: str, dex: str = ""):
        self.calls.append(("user_state", address, dex))
        if self.user_state_error is not None:
            raise self.user_state_error
        return self.state

    def spot_user_state(self, address: str):
        self.calls.append(("spot_user_state", address))
        if self.spot_state_error is not None:
            raise self.spot_state_error
        return self.spot_state

    def frontend_open_orders(self, address: str, dex: str = ""):
        self.calls.append(("frontend_open_orders", address, dex))
        return self.orders

    def query_order_by_oid(self, user: str, oid: int):
        self.calls.append(("query_order_by_oid", user, oid))
        if self.query_error is not None:
            raise self.query_error
        return self.query_result

    def historical_orders(self, user: str):
        self.calls.append(("historical_orders", user))
        return self.history


class FakeExchange:
    """只替代写请求边界，签名逐项对齐 SDK 0.24.0。"""

    def __init__(self) -> None:
        self.order_result = _order_response()
        self.cancel_result = {
            "status": "ok",
            "response": {
                "type": "cancel",
                "data": {"statuses": ["success"]},
            },
        }
        self.calls: list[tuple] = []

    def order(
        self,
        name: str,
        is_buy: bool,
        sz: float,
        limit_px: float,
        order_type: dict,
        reduce_only: bool = False,
        cloid=None,
        builder=None,
    ):
        self.calls.append(
            (
                "order",
                name,
                is_buy,
                sz,
                limit_px,
                order_type,
                reduce_only,
                cloid,
                builder,
            )
        )
        return self.order_result

    def cancel(self, name: str, oid: int):
        self.calls.append(("cancel", name, oid))
        return self.cancel_result


def _client_class():
    """延迟导入，使 RED 阶段每条用例都因目标模块缺失而失败。"""
    module = importlib.import_module("adapters.hyperliquid_client")
    return module.HyperliquidClient


def _client(
    *,
    info: FakeInfo | None = None,
    exchange: FakeExchange | None = None,
    trading_enabled: bool = False,
):
    return _client_class()(
        account_address=ACCOUNT_ADDRESS,
        trading_enabled=trading_enabled,
        info=info or FakeInfo(),
        exchange=exchange or FakeExchange(),
    )


@pytest.mark.parametrize("operation", ["limit", "market", "cancel"])
def test_default_client_rejects_writes_without_sdk_request(operation: str) -> None:
    exchange = FakeExchange()
    client = _client(exchange=exchange)

    if operation == "limit":
        call = client.place_limit_order(
            "BTC", Side.BUY, Decimal("0.001"), Decimal("62000")
        )
    elif operation == "market":
        call = client.market_order("BTC", Side.BUY, Decimal("0.001"))
    else:
        call = client.cancel_order("BTC", 101)

    with pytest.raises(PermissionError, match="只读"):
        asyncio.run(call)
    assert exchange.calls == []


@pytest.mark.parametrize(
    ("account_address", "agent_private_key"),
    [(ACCOUNT_ADDRESS, None), (None, AGENT_PRIVATE_KEY)],
)
def test_trading_enabled_requires_agent_credentials(
    account_address: str | None,
    agent_private_key: str | None,
) -> None:
    client_cls = _client_class()

    with pytest.raises(ValueError, match="代理钱包|账户地址"):
        client_cls(
            account_address=account_address,
            trading_enabled=True,
            agent_private_key=agent_private_key,
        )


@pytest.mark.parametrize(
    ("bid", "ask"),
    [("62500", "62500"), ("62501", "62500"), ("0", "62500"), ("62490", "-1")],
)
def test_market_price_rejects_crossed_or_non_positive_book(
    bid: str,
    ask: str,
) -> None:
    client = _client(info=FakeInfo(book=_book(bid, ask)))

    with pytest.raises(ValueError, match="盘口"):
        asyncio.run(client.get_market_price("BTC"))


def test_mark_price_uses_exchange_mark_field_not_book_mid() -> None:
    info = FakeInfo(
        meta_contexts=_meta_and_contexts(mark_price="62500"),
        book=_book("99", "101"),
    )
    client = _client(info=info)

    mark = asyncio.run(client.get_mark_price("BTC"))

    assert mark == Decimal("62500")
    assert ("l2_snapshot", "BTC") not in info.calls


def test_balance_exposes_positive_equity_attribute() -> None:
    client = _client(info=FakeInfo(state=_user_state()))

    balance = asyncio.run(client.get_balance())

    assert balance.equity == Decimal("125.5")
    assert balance.equity > 0


def test_balance_uses_spot_usdc_when_perps_account_value_is_zero() -> None:
    info = FakeInfo(
        state=_user_state(account_value="0.0", withdrawable="0.0"),
        spot_state=_spot_user_state("457.3100"),
    )
    client = _client(info=info)

    balance = asyncio.run(client.get_balance())

    assert balance.equity == Decimal("457.3100")
    assert balance.perps_account_value == Decimal("0.0")
    assert balance.spot_usdc_total == Decimal("457.3100")


def test_balance_sums_perps_account_value_and_spot_usdc() -> None:
    info = FakeInfo(
        state=_user_state(account_value="100", withdrawable="80"),
        spot_state=_spot_user_state("50"),
    )
    client = _client(info=info)

    balance = asyncio.run(client.get_balance())

    assert balance.equity == Decimal("150")
    assert balance.perps_account_value == Decimal("100")
    assert balance.spot_usdc_total == Decimal("50")


def test_balance_rejects_zero_total_equity() -> None:
    client = _client(
        info=FakeInfo(
            state=_user_state(account_value="0", withdrawable="0"),
            spot_state=_spot_user_state("0"),
        )
    )

    with pytest.raises(ValueError, match="账户权益.*必须为正数"):
        asyncio.run(client.get_balance())


@pytest.mark.parametrize(
    ("failed_side", "expected_equity", "expected_perps", "expected_spot"),
    [
        ("perps", Decimal("50"), Decimal("0"), Decimal("50")),
        ("spot", Decimal("100"), Decimal("100"), Decimal("0")),
    ],
)
def test_balance_uses_successful_side_when_other_query_fails(
    failed_side: str,
    expected_equity: Decimal,
    expected_perps: Decimal,
    expected_spot: Decimal,
) -> None:
    info = FakeInfo(
        state=_user_state(account_value="100", withdrawable="80"),
        spot_state=_spot_user_state("50"),
    )
    if failed_side == "perps":
        info.user_state_error = RuntimeError("永续查询失败")
    else:
        info.spot_state_error = RuntimeError("Spot 查询失败")
    client = _client(info=info)

    balance = asyncio.run(client.get_balance())

    assert balance.equity == expected_equity
    assert balance.perps_account_value == expected_perps
    assert balance.spot_usdc_total == expected_spot


def test_balance_raises_when_both_queries_fail() -> None:
    info = FakeInfo()
    info.user_state_error = RuntimeError("永续查询失败")
    info.spot_state_error = RuntimeError("Spot 查询失败")
    client = _client(info=info)

    with pytest.raises(RuntimeError, match="均失败"):
        asyncio.run(client.get_balance())

    assert ("user_state", ACCOUNT_ADDRESS, "") in info.calls
    assert ("spot_user_state", ACCOUNT_ADDRESS) in info.calls


def test_balance_excludes_non_usdc_spot_assets() -> None:
    spot_state = _spot_user_state(
        "50",
        extra_balances=[
            {"coin": "HYPE", "hold": "1", "total": "999", "entryNtl": "5"},
            {"coin": "MAX", "hold": "2", "total": "888", "entryNtl": "6"},
        ],
    )
    client = _client(
        info=FakeInfo(
            state=_user_state(account_value="0", withdrawable="0"),
            spot_state=spot_state,
        )
    )

    balance = asyncio.run(client.get_balance())

    assert balance.equity == Decimal("50")
    assert balance.spot_usdc_total == Decimal("50")


def test_position_is_zero_when_market_is_absent() -> None:
    client = _client(info=FakeInfo(state=_user_state([])))

    position = asyncio.run(client.get_position("BTC"))

    assert position.market == "BTC"
    assert position.signed_size == 0


def test_positions_keep_exchange_signed_sizes() -> None:
    state = _user_state(
        [_position("BTC", "0.002", "45000"), _position("ETH", "-0.5", "4200")]
    )
    client = _client(info=FakeInfo(state=state))

    btc = asyncio.run(client.get_position("BTC"))
    eth = asyncio.run(client.get_position("ETH"))

    assert btc.signed_size == Decimal("0.002")
    assert eth.signed_size == Decimal("-0.5")


def test_account_wide_positions_are_not_filtered_by_market() -> None:
    state = _user_state(
        [_position("BTC", "0.002", "45000"), _position("ETH", "-0.5", "4200")]
    )
    info = FakeInfo(state=state)
    client = _client(info=info)

    positions = asyncio.run(client.get_all_positions())

    assert [(item.market, item.signed_size) for item in positions] == [
        ("BTC", Decimal("0.002")),
        ("ETH", Decimal("-0.5")),
    ]
    assert info.calls == [("user_state", ACCOUNT_ADDRESS, "")]


def test_account_wide_open_orders_are_not_filtered_by_market() -> None:
    info = FakeInfo(orders=[_open_order("BTC", 101), _open_order("ETH", 202)])
    client = _client(info=info)

    orders = asyncio.run(client.get_all_open_orders())

    assert [(item.market, item.id) for item in orders] == [("BTC", 101), ("ETH", 202)]
    assert info.calls == [("frontend_open_orders", ACCOUNT_ADDRESS, "")]


def test_limit_order_uses_alo_for_post_only() -> None:
    exchange = FakeExchange()
    client = _client(exchange=exchange, trading_enabled=True)

    result = asyncio.run(
        client.place_limit_order(
            "BTC",
            Side.BUY,
            Decimal("0.001234"),
            Decimal("62499.9"),
            post_only=True,
            reduce_only=False,
        )
    )

    call = exchange.calls[0]
    assert call[0:3] == ("order", "BTC", True)
    assert call[5] == {"limit": {"tif": "Alo"}}
    assert call[6] is False
    assert result.id == 101


def test_post_only_business_rejection_becomes_runtime_error() -> None:
    """锁定 SDK 业务响应到真实适配器异常的转换，避免事故桩漂移。"""
    exchange = FakeExchange()
    exchange.order_result = _order_error_response(
        "Post only order would have immediately matched, bbo was 77479@77480. asset=0"
    )
    client = _client(exchange=exchange, trading_enabled=True)

    with pytest.raises(
        RuntimeError,
        match="Post only order would have immediately matched",
    ):
        asyncio.run(
            client.place_limit_order(
                "BTC",
                Side.SELL,
                Decimal("0.001"),
                Decimal("62510"),
                post_only=True,
            )
        )

    assert exchange.calls[0][5] == {"limit": {"tif": "Alo"}}


def test_market_order_uses_ioc_and_forwards_reduce_only() -> None:
    exchange = FakeExchange()
    exchange.order_result = _order_response(state="filled")
    client = _client(exchange=exchange, trading_enabled=True)

    result = asyncio.run(
        client.market_order(
            "BTC", Side.SELL, Decimal("0.001"), reduce_only=True
        )
    )

    call = exchange.calls[0]
    assert call[0:3] == ("order", "BTC", False)
    assert call[5] == {"limit": {"tif": "Ioc"}}
    assert call[6] is True
    assert result.status == "FILLED"


def test_cancel_missing_open_order_is_success_without_write() -> None:
    exchange = FakeExchange()
    client = _client(
        info=FakeInfo(orders=[]), exchange=exchange, trading_enabled=True
    )

    asyncio.run(client.cancel_order("BTC", 404))

    assert exchange.calls == []


def test_cancel_terminal_race_is_treated_as_success() -> None:
    exchange = FakeExchange()
    exchange.cancel_result = {
        "status": "ok",
        "response": {
            "type": "cancel",
            "data": {
                "statuses": ["Order was never placed, already canceled, or filled."]
            },
        },
    }
    client = _client(
        info=FakeInfo(orders=[_open_order("BTC", 101)]),
        exchange=exchange,
        trading_enabled=True,
    )

    asyncio.run(client.cancel_order("BTC", 101))

    assert exchange.calls == [("cancel", "BTC", 101)]


def test_get_order_by_id_returns_none_for_unknown_oid() -> None:
    client = _client(info=FakeInfo(query_result={"status": "unknownOid"}))

    assert asyncio.run(client.get_order_by_id("BTC", 404)) is None


def test_get_order_by_id_propagates_sdk_error() -> None:
    info = FakeInfo()
    info.query_error = RuntimeError("底层读取失败")
    client = _client(info=info)

    with pytest.raises(RuntimeError, match="底层读取失败"):
        asyncio.run(client.get_order_by_id("BTC", 101))


def test_minimum_and_rounding_follow_hyperliquid_precision() -> None:
    client = _client(info=FakeInfo(meta_contexts=_meta_and_contexts("62500")))

    minimum = asyncio.run(client.get_min_order_size("BTC"))
    price = asyncio.run(client.round_price("BTC", Decimal("12345.67")))
    amount = asyncio.run(client.round_amount("BTC", Decimal("0.00123456")))
    tick = asyncio.run(client.get_price_tick_size("BTC"))

    assert minimum == Decimal("0.00016")
    assert minimum > 0
    assert price == Decimal("12346")
    assert amount == Decimal("0.00123")
    assert tick == Decimal("1")


def test_liquidation_info_uses_mark_price_and_position_liquidation_price() -> None:
    state = _user_state([_position("BTC", "0.002", "45000")])
    info = FakeInfo(state=state, meta_contexts=_meta_and_contexts("62500"))
    client = _client(info=info)

    result = asyncio.run(client.get_liquidation_info("BTC"))

    assert result == (Decimal("62500"), Decimal("45000"))


def test_history_is_filtered_normalized_sorted_and_limited() -> None:
    history = [
        _historical_order("BTC", 101),
        _historical_order("ETH", 202),
        _historical_order("BTC", 303, status="canceled"),
    ]
    history[0]["statusTimestamp"] = 100
    history[2]["statusTimestamp"] = 300
    client = _client(info=FakeInfo(history=history))

    orders = asyncio.run(
        client.get_orders_history(
            "BTC", limit=1, order_type="LIMIT", sort="UPDATED_AT"
        )
    )

    assert len(orders) == 1
    assert orders[0].id == 303
    assert orders[0].status == "CANCELLED"


def test_agent_wallet_connects_without_main_wallet_private_key() -> None:
    created: dict[str, object] = {}
    info = FakeInfo()
    exchange = FakeExchange()

    def exchange_factory(
        wallet,
        base_url=None,
        meta=None,
        vault_address=None,
        account_address=None,
        spot_meta=None,
        perp_dexs=None,
        timeout=None,
    ):
        created.update(
            wallet=wallet,
            base_url=base_url,
            meta=meta,
            vault_address=vault_address,
            account_address=account_address,
            spot_meta=spot_meta,
            perp_dexs=perp_dexs,
            timeout=timeout,
        )
        return exchange

    client = _client_class()(
        account_address=ACCOUNT_ADDRESS,
        trading_enabled=True,
        agent_private_key=AGENT_PRIVATE_KEY,
        info=info,
        exchange_factory=exchange_factory,
    )

    asyncio.run(client.connect())

    assert created["account_address"] == ACCOUNT_ADDRESS
    assert created["wallet"].address != ACCOUNT_ADDRESS
    assert created["meta"] == _meta_and_contexts()[0]


def test_credential_doc_names_agent_wallet_environment_without_vault_claim() -> None:
    module = importlib.import_module("adapters.hyperliquid_client")

    assert "HYPERLIQUID_ACCOUNT_ADDRESS" in module.__doc__
    assert "HYPERLIQUID_AGENT_PRIVATE_KEY" in module.__doc__
    assert "HYPERLIQUID_API_URL" in module.__doc__
    assert "主钱包私钥不应写入环境变量或落盘" in module.__doc__
    assert "vault" not in module.__doc__


def test_real_adapter_implements_the_unified_required_methods() -> None:
    client_cls = _client_class()
    required = {
        "connect",
        "close",
        "get_market_price",
        "get_mark_price",
        "get_position",
        "get_all_positions",
        "get_balance",
        "get_open_orders",
        "get_all_open_orders",
        "place_limit_order",
        "market_order",
        "cancel_order",
        "get_order_by_id",
        "get_orders_history",
        "get_min_order_size",
        "get_price_tick_size",
        "round_price",
        "round_amount",
        "get_liquidation_info",
    }

    assert not inspect.isabstract(client_cls)
    assert required <= {name for name in dir(client_cls) if callable(getattr(client_cls, name))}
    assert list(inspect.signature(client_cls.market_order).parameters) == list(
        inspect.signature(ExchangeAdapter.market_order).parameters
    )
    assert list(inspect.signature(client_cls.cancel_order).parameters)[:3] == [
        "self",
        "market",
        "order_id",
    ]


def test_real_sdk_signatures_match_the_stubbed_boundary() -> None:
    """桩依赖的真实 SDK 签名变化时必须直接失败，不能静默漂移。"""
    from hyperliquid.exchange import Exchange
    from hyperliquid.info import Info

    assert list(inspect.signature(Info).parameters) == [
        "base_url",
        "skip_ws",
        "meta",
        "spot_meta",
        "perp_dexs",
        "timeout",
    ]
    assert list(inspect.signature(Info.meta_and_asset_ctxs).parameters) == ["self"]
    assert list(inspect.signature(Info.l2_snapshot).parameters) == ["self", "name"]
    assert list(inspect.signature(Info.user_state).parameters) == [
        "self",
        "address",
        "dex",
    ]
    assert list(inspect.signature(Info.spot_user_state).parameters) == [
        "self",
        "address",
    ]
    assert list(inspect.signature(Info.frontend_open_orders).parameters) == [
        "self",
        "address",
        "dex",
    ]
    assert list(inspect.signature(Info.query_order_by_oid).parameters) == [
        "self",
        "user",
        "oid",
    ]
    assert list(inspect.signature(Info.historical_orders).parameters) == [
        "self",
        "user",
    ]
    assert list(inspect.signature(Exchange).parameters) == [
        "wallet",
        "base_url",
        "meta",
        "vault_address",
        "account_address",
        "spot_meta",
        "perp_dexs",
        "timeout",
    ]
    assert list(inspect.signature(Exchange.order).parameters) == [
        "self",
        "name",
        "is_buy",
        "sz",
        "limit_px",
        "order_type",
        "reduce_only",
        "cloid",
        "builder",
    ]
    assert list(inspect.signature(Exchange.cancel).parameters) == [
        "self",
        "name",
        "oid",
    ]
