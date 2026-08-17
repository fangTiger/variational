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


def test_limit_order_returns_unresolved_order_ref(tmp_path) -> None:
    """第一期不解析 order_index，返回值明确携带 id=None。"""
    signer = _FakeSigner()
    client = _trading_client(tmp_path, signer)

    ref = _run_and_close(
        client,
        client.place_limit_order(
            "BTC", Side.BUY, Decimal("0.001"), Decimal("60000")
        ),
    )

    assert isinstance(ref, OrderRef)
    assert ref.id is None
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


def test_cancel_all_orders_passes_market_and_timestamp(tmp_path) -> None:
    signer = _FakeSigner()
    client = _trading_client(tmp_path, signer)

    _run_and_close(client, client.cancel_all_orders("BTC"))

    name, kwargs = signer.calls[0]
    assert name == "cancel_all_orders"
    assert kwargs["cancel_all_market_index"] == 1
    assert kwargs["time_in_force"] == 0
    assert isinstance(kwargs["timestamp_ms"], int)
    assert kwargs["timestamp_ms"] > 0


def test_enabled_single_cancel_remains_second_phase_feature(tmp_path) -> None:
    """第一期只支持整体撤单，不得悄悄接回单笔订单号依赖链。"""
    client = _trading_client(tmp_path)

    with pytest.raises(NotImplementedError, match="第二期"):
        _run_and_close(client, client.cancel_order("BTC", 12345))


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


def test_get_open_orders_uses_auth_and_returns_list(tmp_path) -> None:
    orders = [{"client_order_index": 1, "order_index": 900, "status": "open"}]
    signer = _FakeSigner()
    client = _trading_client(tmp_path, signer, orders=orders)

    got = _run_and_close(client, client.get_open_orders("BTC"))

    assert got == orders
    assert signer.calls[0][0] == "create_auth_token_with_expiry"
