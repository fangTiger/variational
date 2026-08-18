"""Lighter Robinhood Chain 只读适配器测试。"""

from __future__ import annotations

import asyncio
import inspect
import json
from copy import deepcopy
from decimal import Decimal
from unittest.mock import AsyncMock

import httpx
import pytest

from adapters.base import Side
from adapters.lighter_client import LighterClient
from adapters.order_ref import OrderRef


REAL_L1_ADDRESS = "0x4A3D...3d82"
REAL_ACCOUNT_INDEX = 5626

# 以下三份基准数据来自 Robinhood Chain Lighter 的真实抓取响应。
# 场景测试只替换它明确要验证的字段，避免再次凭空发明响应结构。
REAL_SUB_ACCOUNTS_RESPONSE = {
    "code": 200,
    "l1_address": REAL_L1_ADDRESS,
    "sub_accounts": [
        {
            "code": 0,
            "account_type": 0,
            "index": REAL_ACCOUNT_INDEX,
            "l1_address": REAL_L1_ADDRESS,
            "available_balance": "",
            "status": 1,
            "collateral": "489.231154",
            "total_order_count": 0,
        }
    ],
}

REAL_ACCOUNT_RESPONSE = {
    "code": 200,
    "total": 1,
    "accounts": [
        {
            "index": REAL_ACCOUNT_INDEX,
            "account_index": REAL_ACCOUNT_INDEX,
            "collateral": "489.265906",
            "available_balance": "474.781202",
            "total_asset_value": "489.265906",
            "total_order_count": 0,
            "positions": [
                {
                    "market_id": 1,
                    "symbol": "BTC",
                    "sign": 1,
                    "position": "0.01590",
                    "avg_entry_price": "63028.1",
                    "position_value": "1002.113400",
                    "unrealized_pnl": "-0.032760",
                    "realized_pnl": "0.000000",
                    "liquidation_price": "0",
                    "allocated_margin": "0.000000",
                    "initial_margin_fraction": "20.00",
                    "open_order_count": 0,
                }
            ],
            "assets": [{"symbol": "USDG", "asset_id": 3, "balance": "..."}],
        }
    ],
}

REAL_ORDER_BOOK_DETAILS_RESPONSE = {
    "code": 200,
    "order_book_details": [
        {
            "symbol": "BTC",
            "market_id": 1,
            "mark_price": "63015.5",
            "index_price": "...",
            "last_trade_price": 62906.1,
            "open_interest": 107.79223,
            "daily_quote_token_volume": 116075504.0,
            "taker_fee": "0.0000",
            "maker_fee": "0.0000",
            "min_base_amount": "0.00020",
            "min_quote_amount": "10.000000",
        }
    ],
}


def _real_position(**overrides) -> dict:
    """复制真实 BTC 持仓，并仅覆盖当前场景关心的字段。"""
    position = deepcopy(REAL_ACCOUNT_RESPONSE["accounts"][0]["positions"][0])
    position.update(overrides)
    return position


def _account_response(*, positions: list | None = None) -> dict:
    """复制真实账户详情响应，可替换 positions 以覆盖边界场景。"""
    data = deepcopy(REAL_ACCOUNT_RESPONSE)
    if positions is not None:
        data["accounts"][0]["positions"] = positions
    return data


def _make_client(monkeypatch, handler) -> LighterClient:
    """构造只走 MockTransport 的客户端，确保测试不会触网。"""
    fake_http = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://lighter.test",
    )
    monkeypatch.setattr(httpx, "AsyncClient", lambda **_kwargs: fake_http)
    return LighterClient(l1_address=REAL_L1_ADDRESS, base_url="https://lighter.test")


def _run_and_close(client: LighterClient, awaitable):
    async def exercise():
        try:
            return await awaitable
        finally:
            await client.close()

    return asyncio.run(exercise())


def test_success_code_200_parses_short_position(monkeypatch) -> None:
    """真实成功码 200 必须正常解析空头，不能误报 API 失败。"""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/account"
        assert dict(request.url.params) == {
            "by": "index",
            "value": str(REAL_ACCOUNT_INDEX),
        }
        return httpx.Response(
            200,
            json=_account_response(
                positions=[
                    _real_position(
                        sign=-1,
                        position="0.00020",
                        position_value="12.586280",
                        unrealized_pnl="-0.012500",
                        liquidation_price="66912.223",
                    )
                ]
            ),
        )

    client = _make_client(monkeypatch, handler)
    client.account_index = REAL_ACCOUNT_INDEX
    position = _run_and_close(client, client.get_position("BTC"))

    assert position.signed_size == Decimal("-0.00020")


def test_sign_one_produces_positive_signed_size(monkeypatch) -> None:
    """sign=1 必须解析为多头。"""

    client = _make_client(
        monkeypatch,
        lambda _request: httpx.Response(200, json=deepcopy(REAL_ACCOUNT_RESPONSE)),
    )
    client.account_index = REAL_ACCOUNT_INDEX

    position = _run_and_close(client, client.get_position("BTC"))

    assert position.signed_size == Decimal("0.01590")


def test_zero_position_entry_is_flat(monkeypatch) -> None:
    """响应中的零数量条目必须视为空仓。"""

    client = _make_client(
        monkeypatch,
        lambda _request: httpx.Response(
            200,
            json=_account_response(positions=[_real_position(position="0.00")]),
        ),
    )
    client.account_index = REAL_ACCOUNT_INDEX

    position = _run_and_close(client, client.get_position("BTC"))

    assert position.signed_size == Decimal(0)


def test_missing_symbol_is_flat_without_error(monkeypatch) -> None:
    """成功响应里没有目标 symbol 时返回零仓位，不把它当读取故障。"""

    client = _make_client(
        monkeypatch,
        lambda _request: httpx.Response(
            200,
            json=_account_response(positions=[]),
        ),
    )
    client.account_index = REAL_ACCOUNT_INDEX

    position = _run_and_close(client, client.get_position("BTC"))

    assert position.market == "BTC"
    assert position.signed_size == Decimal(0)
    assert position.raw is None


@pytest.mark.parametrize(
    "positions",
    [
        [None],
        [_real_position(sign=0, position="0.20")],
        [_real_position(sign=1, position="-0.20")],
    ],
)
def test_malformed_position_data_raises_instead_of_returning_flat(
    monkeypatch,
    positions: list,
) -> None:
    """畸形元素、非零仓非法 sign、负数量都不得伪装成空仓。"""

    client = _make_client(
        monkeypatch,
        lambda _request: httpx.Response(
            200,
            json=_account_response(positions=positions),
        ),
    )
    client.account_index = REAL_ACCOUNT_INDEX

    with pytest.raises(ValueError):
        _run_and_close(client, client.get_position("BTC"))


@pytest.mark.parametrize(
    ("failure_kind", "expected_exception"),
    [
        ("timeout", httpx.ReadTimeout),
        ("server-error", httpx.HTTPStatusError),
        ("invalid-json", json.JSONDecodeError),
    ],
)
def test_position_read_failure_raises_instead_of_returning_flat(
    monkeypatch,
    failure_kind: str,
    expected_exception: type[Exception],
) -> None:
    """超时、5xx 或非 JSON 都必须抛出，绝不能伪装成零仓位。"""

    def handler(request: httpx.Request) -> httpx.Response:
        if failure_kind == "timeout":
            raise httpx.ReadTimeout("模拟超时", request=request)
        if failure_kind == "server-error":
            return httpx.Response(503, json={"message": "暂不可用"})
        return httpx.Response(200, text="not-json")

    client = _make_client(monkeypatch, handler)
    client.account_index = REAL_ACCOUNT_INDEX

    with pytest.raises(expected_exception):
        _run_and_close(client, client.get_position("BTC"))


def test_connect_resolves_real_sub_account_index(monkeypatch) -> None:
    """真实 sub_accounts/index 响应必须缓存索引并忽略元素 code 与空余额。"""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/accountsByL1Address"
        assert request.url.params["l1_address"] == REAL_L1_ADDRESS
        return httpx.Response(200, json=deepcopy(REAL_SUB_ACCOUNTS_RESPONSE))

    client = _make_client(monkeypatch, handler)

    _run_and_close(client, client.connect())

    assert client.account_index == REAL_ACCOUNT_INDEX


@pytest.mark.parametrize("container", ["accounts", "account"])
def test_connect_keeps_alternative_account_containers(monkeypatch, container: str) -> None:
    """其他 Lighter 实例的 accounts/account 容器仍保持兼容。"""
    account = deepcopy(REAL_ACCOUNT_RESPONSE["accounts"][0])
    payload = {"code": 200, container: [account] if container == "accounts" else account}
    client = _make_client(
        monkeypatch,
        lambda _request: httpx.Response(200, json=payload),
    )

    _run_and_close(client, client.connect())

    assert client.account_index == REAL_ACCOUNT_INDEX


def test_connect_reports_unopened_address(monkeypatch) -> None:
    """code=21100 必须变成可读的地址未开户错误。"""

    client = _make_client(
        monkeypatch,
        lambda _request: httpx.Response(
            200,
            json={"code": 21100, "message": "account not found"},
        ),
    )

    with pytest.raises(RuntimeError, match="地址未开户"):
        _run_and_close(client, client.connect())


@pytest.mark.parametrize("data", [{}, {"code": None}, {"code": 0}])
def test_legacy_success_codes_remain_accepted(data: dict) -> None:
    """缺失 code、None 和旧成功码 0 仍须保持兼容。"""
    LighterClient._raise_api_error(data, context="兼容性检查")


def _unused_real_success_response(_request: httpx.Request) -> httpx.Response:
    """为不触发 HTTP 的只读能力测试提供真实成功结构。"""
    return httpx.Response(200, json=deepcopy(REAL_ACCOUNT_RESPONSE))


def test_lighter_client_declares_read_only_capability(monkeypatch) -> None:
    """真实 Lighter 适配器必须向引擎声明禁止自动改仓。"""
    client = _make_client(monkeypatch, _unused_real_success_response)

    try:
        assert client.supports_trading is False
    finally:
        asyncio.run(client.close())


def test_trading_disabled_by_default(monkeypatch) -> None:
    """默认只读。这是保护本金腿的最后一道闸，不能靠调用方记得传参。"""
    client = _make_client(monkeypatch, _unused_real_success_response)

    try:
        assert client.trading_enabled is False
    finally:
        asyncio.run(client.close())


def test_supports_trading_follows_trading_enabled(monkeypatch) -> None:
    """引擎读取的 supports_trading 必须与交易开关同源。"""
    ro = _make_client(monkeypatch, _unused_real_success_response)
    rw = LighterClient(
        l1_address="0xabc",
        trading_enabled=True,
        api_private_key="0xkey",
    )

    try:
        assert ro.supports_trading is False
        assert rw.supports_trading is True
    finally:
        asyncio.run(ro.close())
        asyncio.run(rw.close())


def test_market_order_rejected_when_read_only(monkeypatch) -> None:
    """只读实例不允许下市价单。"""
    client = _make_client(monkeypatch, _unused_real_success_response)

    with pytest.raises(PermissionError, match="只读"):
        _run_and_close(
            client,
            client.market_order("BTC", Side.BUY, Decimal("0.1")),
        )


def test_place_limit_order_rejected_when_read_only(monkeypatch) -> None:
    """只读实例不允许挂限价单。"""
    client = _make_client(monkeypatch, _unused_real_success_response)

    with pytest.raises(PermissionError, match="只读"):
        _run_and_close(
            client,
            client.place_limit_order(
                "BTC", Side.BUY, Decimal("0.001"), Decimal("60000")
            ),
        )


def test_cancel_order_rejected_when_read_only(monkeypatch) -> None:
    """保留的单笔撤单契约也必须经过只读闸门。"""
    client = _make_client(monkeypatch, _unused_real_success_response)

    with pytest.raises(PermissionError, match="只读"):
        _run_and_close(client, client.cancel_order("BTC", 1))


def test_cancel_all_orders_rejected_when_read_only(monkeypatch) -> None:
    """只读实例不允许按市场整体撤单。"""
    client = _make_client(monkeypatch, _unused_real_success_response)

    with pytest.raises(PermissionError, match="只读"):
        _run_and_close(client, client.cancel_all_orders("BTC"))


def test_close_position_rejected_when_read_only(monkeypatch) -> None:
    """只读实例不允许机器人自动平仓。"""
    client = _make_client(monkeypatch, _unused_real_success_response)

    with pytest.raises(PermissionError, match="只读"):
        _run_and_close(client, client.close_position("BTC"))


def test_trading_enabled_requires_key() -> None:
    """开了交易开关却没给密钥，必须在构造时立即失败。"""
    with pytest.raises(ValueError, match="api_private_key"):
        LighterClient(l1_address="0xabc", trading_enabled=True)


def test_market_price_uses_mark_price(monkeypatch) -> None:
    """订单簿详情没有 bid/ask 时，应以标记价构造统一行情。"""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/orderBookDetails"
        return httpx.Response(200, json=deepcopy(REAL_ORDER_BOOK_DETAILS_RESPONSE))

    client = _make_client(monkeypatch, handler)

    price = _run_and_close(client, client.get_market_price("BTC"))

    assert price.market == "BTC"
    assert price.bid == Decimal("63015.5")
    assert price.ask == Decimal("63015.5")
    assert price.mid == Decimal("63015.5")


def test_liquidation_info_uses_position_and_mark_price(monkeypatch) -> None:
    """有仓位且清算价非零时返回标记价与清算价。"""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/account":
            return httpx.Response(
                200,
                json=_account_response(
                    positions=[
                        _real_position(
                            sign=-1,
                            position="0.2",
                            liquidation_price="66912.223",
                        )
                    ]
                ),
            )
        assert request.url.path == "/api/v1/orderBookDetails"
        return httpx.Response(200, json=deepcopy(REAL_ORDER_BOOK_DETAILS_RESPONSE))

    client = _make_client(monkeypatch, handler)
    client.account_index = REAL_ACCOUNT_INDEX

    info = _run_and_close(client, client.get_liquidation_info("BTC"))

    assert info == (Decimal("63015.5"), Decimal("66912.223"))


@pytest.mark.parametrize(
    "position",
    [
        _real_position(position="0.00", liquidation_price="0"),
        _real_position(position="0.20", liquidation_price="0"),
    ],
)
def test_liquidation_info_is_none_without_live_liquidation_price(
    monkeypatch,
    position: dict,
) -> None:
    """无仓或清算价为零时没有可用清算信息。"""

    client = _make_client(
        monkeypatch,
        lambda _request: httpx.Response(
            200,
            json=_account_response(positions=[position]),
        ),
    )
    client.account_index = REAL_ACCOUNT_INDEX

    info = _run_and_close(client, client.get_liquidation_info("BTC"))

    assert info is None


def test_get_collateral_reads_account_field(monkeypatch):
    """抵押品取自 accounts[0].collateral，真实响应结构。"""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"code": 200, "total": 1, "accounts": [
                {"index": 5626, "collateral": "489.265906", "positions": []}
            ]},
        )

    client = _make_client(monkeypatch, handler)
    client.account_index = 5626
    assert asyncio.run(client.get_collateral()) == Decimal("489.265906")


class _FakeSendTx:
    """真实 RespSendTx 没有 order_index，假对象也不得凭空添加。"""

    def __init__(self, code=200, message=""):
        self.code = code
        self.message = message
        self.tx_hash = "0xdead"


class _FakeSigner:
    """假签名器；各方法签名与锁定版本的真实 SDK 一致。"""

    ORDER_TYPE_LIMIT = 0
    ORDER_TYPE_MARKET = 1
    ORDER_TIME_IN_FORCE_IMMEDIATE_OR_CANCEL = 0
    ORDER_TIME_IN_FORCE_GOOD_TILL_TIME = 1
    ORDER_TIME_IN_FORCE_POST_ONLY = 2

    def __init__(self, err=None, auth_err=None, response_code=200):
        self.calls = []
        self._err = err
        self._auth_err = auth_err
        self._response_code = response_code

    async def create_order(
        self,
        market_index,
        client_order_index,
        base_amount,
        price,
        is_ask,
        order_type,
        time_in_force,
        reduce_only=False,
        trigger_price=0,
        order_expiry=-1,
        *,
        integrator_account_index=0,
        integrator_taker_fee=0,
        integrator_maker_fee=0,
        self_trade_behavior_mode=0,
        self_trade_equality_mode=0,
        skip_nonce=0,
        nonce=-1,
        api_key_index=255,
    ):
        del (
            trigger_price,
            order_expiry,
            integrator_account_index,
            integrator_taker_fee,
            integrator_maker_fee,
            self_trade_behavior_mode,
            self_trade_equality_mode,
            skip_nonce,
            nonce,
        )
        self.calls.append(
            (
                "create_order",
                {
                    "market_index": market_index,
                    "client_order_index": client_order_index,
                    "base_amount": base_amount,
                    "price": price,
                    "is_ask": is_ask,
                    "order_type": order_type,
                    "time_in_force": time_in_force,
                    "reduce_only": reduce_only,
                    "api_key_index": api_key_index,
                },
            )
        )
        if self._err is not None:
            return None, None, self._err
        return object(), _FakeSendTx(self._response_code, "交易所拒单"), None

    async def create_market_order(
        self,
        market_index,
        client_order_index,
        base_amount,
        avg_execution_price,
        is_ask,
        reduce_only=False,
        *,
        integrator_account_index=0,
        integrator_taker_fee=0,
        integrator_maker_fee=0,
        self_trade_behavior_mode=0,
        self_trade_equality_mode=0,
        skip_nonce=0,
        nonce=-1,
        api_key_index=255,
    ):
        del (
            integrator_account_index,
            integrator_taker_fee,
            integrator_maker_fee,
            self_trade_behavior_mode,
            self_trade_equality_mode,
            skip_nonce,
            nonce,
        )
        self.calls.append(
            (
                "create_market_order",
                {
                    "market_index": market_index,
                    "client_order_index": client_order_index,
                    "base_amount": base_amount,
                    "avg_execution_price": avg_execution_price,
                    "is_ask": is_ask,
                    "reduce_only": reduce_only,
                    "api_key_index": api_key_index,
                },
            )
        )
        if self._err is not None:
            return None, None, self._err
        return object(), _FakeSendTx(self._response_code, "交易所拒单"), None

    async def create_sl_order(
        self,
        market_index,
        client_order_index,
        base_amount,
        trigger_price,
        price,
        is_ask,
        reduce_only=False,
        *,
        integrator_account_index: int = 0,
        integrator_taker_fee: int = 0,
        integrator_maker_fee: int = 0,
        skip_nonce: int = 0,
        nonce: int = -1,
        api_key_index: int = 255,
    ):
        del (
            integrator_account_index,
            integrator_taker_fee,
            integrator_maker_fee,
            skip_nonce,
            nonce,
        )
        self.calls.append(
            (
                "create_sl_order",
                {
                    "market_index": market_index,
                    "client_order_index": client_order_index,
                    "base_amount": base_amount,
                    "trigger_price": trigger_price,
                    "price": price,
                    "is_ask": is_ask,
                    "reduce_only": reduce_only,
                    "api_key_index": api_key_index,
                },
            )
        )
        if self._err is not None:
            return None, None, self._err
        return object(), _FakeSendTx(self._response_code, "交易所拒单"), None

    async def cancel_order(
        self,
        market_index,
        order_index,
        skip_nonce: int = 0,
        nonce: int = -1,
        api_key_index: int = 255,
    ):
        del skip_nonce, nonce
        self.calls.append(
            (
                "cancel_order",
                {
                    "market_index": market_index,
                    "order_index": order_index,
                    "api_key_index": api_key_index,
                },
            )
        )
        if self._err is not None:
            return None, None, self._err
        return object(), _FakeSendTx(self._response_code, "交易所拒单"), None

    async def cancel_all_orders(
        self,
        time_in_force,
        timestamp_ms,
        cancel_all_market_index=255,
        skip_nonce=0,
        nonce=-1,
        api_key_index=255,
    ):
        del skip_nonce, nonce
        self.calls.append(
            (
                "cancel_all_orders",
                {
                    "time_in_force": time_in_force,
                    "timestamp_ms": timestamp_ms,
                    "cancel_all_market_index": cancel_all_market_index,
                    "api_key_index": api_key_index,
                },
            )
        )
        if self._err is not None:
            return None, None, self._err
        return object(), _FakeSendTx(self._response_code, "交易所拒单"), None

    def create_auth_token_with_expiry(
        self,
        deadline=-1,
        *,
        timestamp=None,
        api_key_index=255,
    ):
        self.calls.append(
            (
                "create_auth_token_with_expiry",
                {
                    "deadline": deadline,
                    "timestamp": timestamp,
                    "api_key_index": api_key_index,
                },
            )
        )
        if self._auth_err is not None:
            return None, self._auth_err
        return "auth-token", None


@pytest.mark.parametrize(
    "method_name",
    [
        "create_order",
        "create_market_order",
        "create_sl_order",
        "cancel_order",
        "cancel_all_orders",
        "create_auth_token_with_expiry",
    ],
)
def test_fake_signer_signature_matches_real_sdk(method_name: str) -> None:
    """假对象必须钉住真实参数名、顺序、参数类型和默认值。"""
    import lighter

    def shape(method):
        return [
            (param.name, param.kind, param.default)
            for param in inspect.signature(method).parameters.values()
        ]

    assert shape(getattr(_FakeSigner, method_name)) == shape(
        getattr(lighter.SignerClient, method_name)
    )


def _trading_client(
    tmp_path,
    signer=None,
    orders=None,
    positions=None,
    history=None,
    *,
    api_key_index: int = 255,
) -> LighterClient:
    """构造可交易客户端；所有请求都由 MockTransport 接管。"""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/orderBooks":
            return httpx.Response(
                200,
                json={
                    "code": 200,
                    "order_books": [
                        {
                            "symbol": "BTC",
                            "market_id": 1,
                            "supported_size_decimals": 5,
                            "supported_price_decimals": 1,
                            "min_base_amount": "0.00020",
                            "min_quote_amount": "10.000000",
                        }
                    ],
                },
            )
        if request.url.path == "/api/v1/orderBookDetails":
            return httpx.Response(200, json=deepcopy(REAL_ORDER_BOOK_DETAILS_RESPONSE))
        if request.url.path == "/api/v1/account":
            return httpx.Response(
                200,
                json=_account_response(
                    positions=positions if positions is not None else []
                ),
            )
        if request.url.path == "/api/v1/accountActiveOrders":
            assert request.headers["Authorization"] == "auth-token"
            assert dict(request.url.params) == {
                "account_index": str(REAL_ACCOUNT_INDEX),
                "market_id": "1",
            }
            return httpx.Response(200, json={"code": 200, "orders": orders or []})
        if request.url.path == "/api/v1/accountInactiveOrders":
            assert request.headers["Authorization"] == "auth-token"
            return httpx.Response(
                200,
                json={"code": 200, "orders": history or [], "next_cursor": ""},
            )
        raise AssertionError(f"测试未声明的 HTTP 请求：{request.url}")

    client = LighterClient(
        l1_address="0xabc",
        trading_enabled=True,
        api_private_key="0xkey",
        api_key_index=api_key_index,
        account_index=REAL_ACCOUNT_INDEX,
        client_order_index_path=str(tmp_path / "coi.json"),
    )
    asyncio.run(client._http.aclose())
    client._http = httpx.AsyncClient(
        base_url=client.base_url,
        transport=httpx.MockTransport(handler),
    )
    client._signer = signer or _FakeSigner()
    return client


def test_limit_order_scales_to_integers(tmp_path) -> None:
    """最高风险点：0.00317 → 317，63400.0 → 634000。"""
    signer = _FakeSigner()
    client = _trading_client(tmp_path, signer)

    _run_and_close(
        client,
        client.place_limit_order(
            "BTC", Side.BUY, Decimal("0.00317"), Decimal("63400.0")
        ),
    )

    _, kwargs = signer.calls[0]
    assert kwargs["base_amount"] == 317
    assert kwargs["price"] == 634000
    assert kwargs["market_index"] == 1


def test_limit_order_direction_matches_side(tmp_path) -> None:
    """买单不是 ask，卖单才是 ask。"""
    signer = _FakeSigner()
    client = _trading_client(tmp_path, signer)

    async def place_both():
        await client.place_limit_order(
            "BTC", Side.BUY, Decimal("0.001"), Decimal("60000")
        )
        await client.place_limit_order(
            "BTC", Side.SELL, Decimal("0.001"), Decimal("60000")
        )

    _run_and_close(client, place_both())

    assert signer.calls[0][1]["is_ask"] is False
    assert signer.calls[1][1]["is_ask"] is True


def test_limit_order_uses_requested_time_in_force(tmp_path) -> None:
    signer = _FakeSigner()
    client = _trading_client(tmp_path, signer)

    async def place_both():
        await client.place_limit_order(
            "BTC", Side.BUY, Decimal("0.001"), Decimal("60000"), post_only=True
        )
        await client.place_limit_order(
            "BTC", Side.BUY, Decimal("0.001"), Decimal("60000"), post_only=False
        )

    _run_and_close(client, place_both())

    assert signer.calls[0][1]["time_in_force"] == 2
    assert signer.calls[1][1]["time_in_force"] == 1


def test_limit_order_error_in_third_element_raises(tmp_path) -> None:
    """SDK 把交易错误放在返回三元组第三个元素。"""
    client = _trading_client(tmp_path, _FakeSigner(err="insufficient margin"))

    with pytest.raises(RuntimeError, match="insufficient margin"):
        _run_and_close(
            client,
            client.place_limit_order(
                "BTC", Side.BUY, Decimal("0.001"), Decimal("60000")
            ),
        )


def test_limit_order_non_success_response_code_raises(tmp_path) -> None:
    """SDK 可能以 err=None 返回业务拒单，必须继续检查响应 code。"""
    client = _trading_client(tmp_path, _FakeSigner(response_code=400))

    with pytest.raises(RuntimeError, match="code=400.*交易所拒单"):
        _run_and_close(
            client,
            client.place_limit_order(
                "BTC", Side.BUY, Decimal("0.001"), Decimal("60000")
            ),
        )


def test_limit_order_returns_client_order_index_as_id(tmp_path) -> None:
    """id 必须是自己分配的 client_order_index，且不得为 None。

    引擎在返回值缺 id 时判定挂单失败并原格重挂（grid_engine.py:1770），
    而订单其实已挂在交易所上。
    """
    signer = _FakeSigner()
    client = _trading_client(tmp_path, signer)

    ref = _run_and_close(
        client,
        client.place_limit_order(
            "BTC", Side.BUY, Decimal("0.001"), Decimal("60000")
        ),
    )

    assert isinstance(ref, OrderRef)
    assert ref.id == 1
    assert ref.client_order_index == 1
    assert not hasattr(_FakeSendTx(), "order_index")


def test_client_order_index_increments(tmp_path) -> None:
    signer = _FakeSigner()
    client = _trading_client(tmp_path, signer)

    async def place_both():
        await client.place_limit_order(
            "BTC", Side.BUY, Decimal("0.001"), Decimal("60000")
        )
        await client.place_limit_order(
            "BTC", Side.BUY, Decimal("0.001"), Decimal("60000")
        )

    _run_and_close(client, place_both())

    first = signer.calls[0][1]["client_order_index"]
    second = signer.calls[1][1]["client_order_index"]
    assert second == first + 1


def test_transaction_calls_leave_nonce_management_to_sdk(tmp_path) -> None:
    """非默认 key 也不得显式传 key/nonce，否则会绕过 SDK nonce manager。"""
    signer = _FakeSigner()
    signer.create_order = AsyncMock(wraps=signer.create_order)
    signer.create_market_order = AsyncMock(wraps=signer.create_market_order)
    signer.cancel_all_orders = AsyncMock(wraps=signer.cancel_all_orders)
    client = _trading_client(tmp_path, signer, api_key_index=7)

    async def submit_all():
        await client.place_limit_order(
            "BTC", Side.BUY, Decimal("0.001"), Decimal("60000")
        )
        await client.market_order("BTC", Side.BUY, Decimal("0.001"))
        await client.cancel_all_orders("BTC")

    _run_and_close(client, submit_all())

    for method in (
        signer.create_order,
        signer.create_market_order,
        signer.cancel_all_orders,
    ):
        assert "api_key_index" not in method.await_args.kwargs
        assert "nonce" not in method.await_args.kwargs


def test_cancel_all_orders_uses_nil_timestamp_for_ioc(tmp_path) -> None:
    """IOC 模式下 CancelAllTime 必须为 nil，否则交易所整体撤单必失败。

    实盘验证：传非零 timestamp_ms 时交易所返回
    「CancelAllTime should be nil」，传 0 才返回 code=200。
    timestamp_ms 只在 SCHEDULED 模式下有意义。
    """
    signer = _FakeSigner()
    client = _trading_client(tmp_path, signer)

    _run_and_close(client, client.cancel_all_orders("BTC"))

    name, kwargs = signer.calls[0]
    assert name == "cancel_all_orders"
    assert kwargs["cancel_all_market_index"] == 1
    assert kwargs["time_in_force"] == signer.ORDER_TIME_IN_FORCE_IMMEDIATE_OR_CANCEL
    assert kwargs["timestamp_ms"] == 0


def test_require_signer_prepares_ssl_cert(tmp_path, monkeypatch) -> None:
    """构造签名器前必须先兜底 CA 证书，否则所有写操作会 SSL 失败。

    Lighter SDK 内部用 aiohttp 提交交易，读路径的 httpx 自带 certifi
    不受影响，因此缺证书只会打挂写操作——症状与地域封锁难以区分。
    入口文件各自调用 ensure_ssl_cert() 不可靠，新入口一旦遗漏就复发，
    故由适配器自身保证。
    """
    called: list[bool] = []
    monkeypatch.setattr(
        "adapters.lighter_client.ensure_ssl_cert",
        lambda: called.append(True),
    )
    client = _trading_client(tmp_path, _FakeSigner())
    client._signer = None  # 强制走真实的懒加载分支

    import lighter

    monkeypatch.setattr(lighter, "SignerClient", lambda **kwargs: _FakeSigner())
    client._require_signer()

    assert called == [True]


def test_single_cancel_accepts_string_client_index(tmp_path) -> None:
    """引擎可能把 id 存成字符串（如经 JSON 状态文件回环）。

    匹配前必须归一成整数，否则字符串与整数比不相等，
    每次撤单都会静默走"已不在挂单簿上"分支，订单永远撤不掉。
    """
    signer = _FakeSigner()
    client = _trading_client(
        tmp_path,
        signer,
        orders=[_raw_order(order_index=844424907205585, client_order_index=42)],
    )

    _run_and_close(client, client.cancel_order("BTC", "42"))

    calls = [c for c in signer.calls if c[0] == "cancel_order"]
    assert len(calls) == 1
    assert calls[0][1]["order_index"] == 844424907205585
    assert isinstance(calls[0][1]["order_index"], int)


def test_enabled_close_position_uses_reduce_only_market_order(tmp_path) -> None:
    """supports_trading=True 时，紧急平仓路径必须真的能平掉 primary。"""
    signer = _FakeSigner()
    client = _trading_client(
        tmp_path,
        signer,
        positions=[_real_position(sign=1, position="0.00317")],
    )

    _run_and_close(client, client.close_position("BTC"))

    name, kwargs = signer.calls[0]
    assert name == "create_market_order"
    assert kwargs["base_amount"] == 317
    assert kwargs["is_ask"] is True
    assert kwargs["reduce_only"] is True


@pytest.mark.parametrize(
    ("side", "expected_price", "expected_is_ask"),
    [
        (Side.BUY, 636456, False),
        (Side.SELL, 623853, True),
    ],
)
def test_market_order_applies_directional_ioc_price_limit(
    tmp_path,
    side: Side,
    expected_price: int,
    expected_is_ask: bool,
) -> None:
    """IOC 市价单以标记价为基准，按方向放宽 1% 执行边界。"""
    signer = _FakeSigner()
    client = _trading_client(tmp_path, signer)

    _run_and_close(
        client,
        client.market_order(
            "BTC", side, Decimal("0.00317"), reduce_only=True
        ),
    )

    name, kwargs = signer.calls[0]
    assert name == "create_market_order"
    assert kwargs["base_amount"] == 317
    assert kwargs["avg_execution_price"] == expected_price
    assert kwargs["is_ask"] is expected_is_ask
    assert kwargs["reduce_only"] is True


def _raw_order(**overrides) -> dict:
    """实测形状的 Lighter 订单，可局部覆盖。"""
    base = {
        "order_index": 900,
        "client_order_index": 1,
        "status": "open",
        "price": "64000.0",
        "is_ask": False,
        "initial_base_amount": "0.00300",
        "filled_base_amount": "0.00000",
        "reduce_only": False,
        "type": "limit",
        "side": "",
        "created_at": 1787052005,
    }
    base.update(overrides)
    return base


def test_get_open_orders_returns_engine_readable_views(tmp_path) -> None:
    """必须返回属性视图而非裸 dict。

    引擎与 filter_grid_orders 全部走 getattr；裸 dict 会让 reduce_only
    恒取默认值 False，把交易所端保护单当成普通网格单撤掉。
    """
    signer = _FakeSigner()
    client = _trading_client(
        tmp_path, signer, orders=[_raw_order(reduce_only=True, is_ask=True)]
    )

    got = _run_and_close(client, client.get_open_orders("BTC"))

    assert len(got) == 1
    assert got[0].id == 1  # client_order_index，引擎侧身份
    assert got[0].order_index == 900  # 交易所侧，仅撤单签名用
    assert got[0].reduce_only is True
    assert got[0].side == Side.SELL.value
    assert signer.calls[0][0] == "create_auth_token_with_expiry"


def test_get_orders_history_reads_inactive_orders(tmp_path) -> None:
    """历史订单走 accountInactiveOrders，供引擎判终态。"""
    client = _trading_client(
        tmp_path,
        _FakeSigner(),
        history=[_raw_order(status="filled", filled_base_amount="0.00300")],
    )

    got = _run_and_close(client, client.get_orders_history("BTC", limit=10))

    assert len(got) == 1
    assert got[0].status == "FILLED"
    assert got[0].filled_qty == Decimal("0.00300")


def test_cancel_order_resolves_client_index_to_exchange_index(tmp_path) -> None:
    """引擎传的是 client_order_index，签名要的是 order_index。

    两者不是一回事，直接把 client_order_index 交给 SDK 会撤到别人的单
    或签名失败。
    """
    signer = _FakeSigner()
    client = _trading_client(
        tmp_path,
        signer,
        orders=[
            _raw_order(order_index=844424907205585, client_order_index=42),
            _raw_order(order_index=999999999999999, client_order_index=43),
        ],
    )

    _run_and_close(client, client.cancel_order("BTC", 42))

    calls = [c for c in signer.calls if c[0] == "cancel_order"]
    assert len(calls) == 1
    assert calls[0][1]["market_index"] == 1
    assert calls[0][1]["order_index"] == 844424907205585
    assert isinstance(calls[0][1]["order_index"], int)


def test_cancel_order_of_vanished_order_is_a_noop(tmp_path) -> None:
    """撤一个已不在挂单簿上的单必须当成功。

    引擎撤单失败时保留记录下轮重试（grid_engine.py:1809）；
    若这里抛错，已成交的订单会被无限重试撤单，形成活锁。
    """
    signer = _FakeSigner()
    client = _trading_client(tmp_path, signer, orders=[])

    _run_and_close(client, client.cancel_order("BTC", 42))

    assert [c for c in signer.calls if c[0] == "cancel_order"] == []


def test_cancel_grid_orders_spares_protective_orders(tmp_path) -> None:
    """只撤普通网格单，reduce_only 与条件单必须留下。

    撤错会让整仓止损消失，网格在急跌里失去最后一道保护。
    """
    signer = _FakeSigner()
    client = _trading_client(
        tmp_path,
        signer,
        orders=[
            _raw_order(order_index=901, reduce_only=False, type="limit"),
            _raw_order(order_index=902, reduce_only=True, type="limit"),
            _raw_order(order_index=903, reduce_only=False, type="tpsl"),
        ],
    )

    count = _run_and_close(client, client.cancel_grid_orders("BTC"))

    cancelled = [c[1]["order_index"] for c in signer.calls if c[0] == "cancel_order"]
    assert cancelled == [901]
    assert count == 1


def test_limit_order_below_min_quote_amount_rejected_locally(tmp_path) -> None:
    """名义额低于 min_quote_amount 必须本地拦下，不能发给交易所。

    实盘验证：Lighter 除最小数量 0.00020 BTC 外还有最小名义额 $10，
    低于门槛返回 code=21706 'invalid order base or quote amount'。
    若不本地拦截，配置里每格金额切得过小时网格会"看起来在跑"却每笔
    都被拒，且错误码不说明是哪个门槛，极难定位。
    """
    signer = _FakeSigner()
    client = _trading_client(tmp_path, signer)

    # 0.00020 BTC × 44000 = $8.8 < $10
    with pytest.raises(ValueError, match="最小名义额"):
        _run_and_close(
            client,
            client.place_limit_order(
                "BTC", Side.BUY, Decimal("0.00020"), Decimal("44000")
            ),
        )

    assert [c for c in signer.calls if c[0] == "create_order"] == []


def test_limit_order_at_min_quote_amount_is_accepted(tmp_path) -> None:
    """刚好达到门槛必须放行，不能把边界值一并拒掉。"""
    signer = _FakeSigner()
    client = _trading_client(tmp_path, signer)

    # 0.00020 BTC × 50000 = $10.00
    _run_and_close(
        client,
        client.place_limit_order(
            "BTC", Side.BUY, Decimal("0.00020"), Decimal("50000")
        ),
    )

    assert [c for c in signer.calls if c[0] == "create_order"]


def test_place_limit_order_returns_usable_id_synchronously(tmp_path) -> None:
    """挂单必须同步返回可用订单号。

    Lighter 下单响应只有 tx_hash，若返回 id=None，引擎会判定挂单失败
    并原格重挂（grid_engine.py:1770），而订单其实已挂在交易所上——
    形成孤儿单加重复挂单。故返回自己分配的 client_order_index。
    """
    signer = _FakeSigner()
    client = _trading_client(tmp_path, signer)

    ref = _run_and_close(
        client,
        client.place_limit_order("BTC", Side.BUY, Decimal("0.001"), Decimal("60000")),
    )

    assert ref.id is not None
    assert ref.id == ref.client_order_index
    name, kwargs = signer.calls[0]
    assert name == "create_order"
    assert kwargs["client_order_index"] == ref.id


def _sl_order(**overrides) -> dict:
    """reduce-only 且带触发价的整仓止损单。"""
    fields = {
        "order_index": 7001,
        "client_order_index": 70,
        "reduce_only": True,
        "trigger_price": "61000.0",
        "is_ask": True,
        "type": "stop-loss",
    }
    fields.update(overrides)
    return _raw_order(**fields)


def test_position_stop_loss_rejects_flat_and_bad_trigger(tmp_path) -> None:
    """空仓或非法触发价必须本地拒绝，与 Extended 侧语义一致。"""
    client = _trading_client(tmp_path, _FakeSigner())

    with pytest.raises(ValueError, match="空仓"):
        _run_and_close(
            client,
            client.place_position_stop_loss("BTC", Decimal("0"), Decimal("61000")),
        )

    client = _trading_client(tmp_path, _FakeSigner())
    with pytest.raises(ValueError, match="触发价"):
        _run_and_close(
            client,
            client.place_position_stop_loss("BTC", Decimal("0.01"), Decimal("0")),
        )


@pytest.mark.parametrize(
    ("signed_size", "expect_is_ask"),
    [(Decimal("0.01"), True), (Decimal("-0.01"), False)],
)
def test_position_stop_loss_closes_in_correct_direction(
    tmp_path, signed_size: Decimal, expect_is_ask: bool
) -> None:
    """多仓止损必须是卖出、空仓止损必须是买入。

    方向反了，止损单会在触发时加倍放大敞口而不是平仓。
    """
    signer = _FakeSigner()
    client = _trading_client(tmp_path, signer)

    _run_and_close(
        client,
        client.place_position_stop_loss("BTC", signed_size, Decimal("61000")),
    )

    call = [c for c in signer.calls if c[0] == "create_sl_order"][0][1]
    assert call["is_ask"] is expect_is_ask
    assert call["reduce_only"] is True
    assert call["base_amount"] == 1000  # abs(0.01) → 5 位精度


def test_position_stop_loss_prices_allow_ioc_execution(tmp_path) -> None:
    """止损是 IOC 单，限价必须留出滑点空间朝平仓方向偏。

    平多（卖出）时限价高于触发价就永远吃不到，止损形同虚设。
    """
    signer = _FakeSigner()
    client = _trading_client(tmp_path, signer)

    _run_and_close(
        client,
        client.place_position_stop_loss("BTC", Decimal("0.01"), Decimal("61000")),
    )

    call = [c for c in signer.calls if c[0] == "create_sl_order"][0][1]
    assert call["trigger_price"] == 610000  # 1 位精度
    assert call["price"] < call["trigger_price"]


def test_position_stop_loss_replaces_instead_of_accumulating(tmp_path) -> None:
    """重挂前必须撤掉旧止损单。

    Extended 的整仓 TPSL 是单例，重挂即替换；Lighter 的 create_sl_order
    每次新建一张。而引擎在持仓变化时就会重挂（grid_engine.py:519），
    网格每笔成交都改变持仓——不撤旧单会累积出成百上千张陈旧止损单，
    触发时一起打出去，把平仓变成反向开仓。
    """
    signer = _FakeSigner()
    client = _trading_client(tmp_path, signer, orders=[_sl_order()])

    _run_and_close(
        client,
        client.place_position_stop_loss("BTC", Decimal("0.01"), Decimal("61000")),
    )

    names = [c[0] for c in signer.calls]
    assert names.index("cancel_order") < names.index("create_sl_order")
    cancelled = [c[1]["order_index"] for c in signer.calls if c[0] == "cancel_order"]
    assert cancelled == [7001]


def test_cancel_tpsl_spares_ordinary_grid_orders(tmp_path) -> None:
    """只撤整仓止损，普通网格单必须留下。"""
    signer = _FakeSigner()
    client = _trading_client(
        tmp_path,
        signer,
        orders=[_raw_order(order_index=801), _sl_order(order_index=802)],
    )

    _run_and_close(client, client.cancel_tpsl("BTC"))

    cancelled = [c[1]["order_index"] for c in signer.calls if c[0] == "cancel_order"]
    assert cancelled == [802]


def test_get_position_tpsl_exposes_trigger_price(tmp_path) -> None:
    """引擎读 .trigger_price 来校验交易所侧止损是否还在（grid_engine.py:537）。"""
    client = _trading_client(
        tmp_path, _FakeSigner(), orders=[_raw_order(order_index=801), _sl_order()]
    )

    got = _run_and_close(client, client.get_position_tpsl("BTC"))

    assert got is not None
    assert got.trigger_price == Decimal("61000.0")


def test_get_position_tpsl_returns_none_when_absent(tmp_path) -> None:
    """没有止损单时返回 None，不能把普通网格单误判成止损。"""
    client = _trading_client(tmp_path, _FakeSigner(), orders=[_raw_order()])

    assert _run_and_close(client, client.get_position_tpsl("BTC")) is None


def test_round_price_aligns_to_market_tick(tmp_path) -> None:
    """必须按市场精度对齐价格。

    引擎在翻单重试对账时用 round_price 把目标价与交易所返回价对齐比较
    （grid_engine.py:1131）。基类默认原样返回，比较必然失配，引擎会
    误判自己的单不存在而重复挂单。
    """
    client = _trading_client(tmp_path, _FakeSigner())

    got = _run_and_close(client, client.round_price("BTC", Decimal("64123.456")))

    assert got == Decimal("64123.5")  # supported_price_decimals=1


def test_round_amount_aligns_to_market_step(tmp_path) -> None:
    """数量同理，按 supported_size_decimals 对齐。"""
    client = _trading_client(tmp_path, _FakeSigner())

    got = _run_and_close(client, client.round_amount("BTC", Decimal("0.0012345678")))

    assert got == Decimal("0.00123")  # supported_size_decimals=5


def test_price_tick_size_from_market_meta(tmp_path) -> None:
    """引擎用 tick/2 作为整仓 TPSL 触发价的比对容差（grid_engine.py:549）。

    返回 None 会让容差退化，止损单被反复判定为"和交易所不一致"而重挂。
    """
    client = _trading_client(tmp_path, _FakeSigner())

    assert _run_and_close(client, client.get_price_tick_size("BTC")) == Decimal("0.1")
