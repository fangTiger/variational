"""Lighter Robinhood Chain 只读适配器测试。"""

from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from decimal import Decimal

import httpx
import pytest

from adapters.base import Side
from adapters.lighter_client import LighterClient


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


def test_market_order_is_intentionally_unsupported(monkeypatch) -> None:
    """人工腿不允许机器人下市价单。"""
    client = _make_client(monkeypatch, _unused_real_success_response)

    with pytest.raises(NotImplementedError, match="人工"):
        _run_and_close(
            client,
            client.market_order("BTC", Side.BUY, Decimal("0.1")),
        )


def test_close_position_is_intentionally_unsupported(monkeypatch) -> None:
    """人工腿不允许机器人自动平仓。"""
    client = _make_client(monkeypatch, _unused_real_success_response)

    with pytest.raises(NotImplementedError, match="人工"):
        _run_and_close(client, client.close_position("BTC"))


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
