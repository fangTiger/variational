"""Lighter Robinhood Chain 只读适配器测试。"""

from __future__ import annotations

import asyncio
import json
from decimal import Decimal

import httpx
import pytest

from adapters.base import Side
from adapters.lighter_client import LighterClient


def _make_client(monkeypatch, handler) -> LighterClient:
    """构造只走 MockTransport 的客户端，确保测试不会触网。"""
    fake_http = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://lighter.test",
    )
    monkeypatch.setattr(httpx, "AsyncClient", lambda **_kwargs: fake_http)
    return LighterClient(l1_address="0xabc", base_url="https://lighter.test")


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
        return httpx.Response(
            200,
            json={
                "code": 200,
                "positions": [
                    {
                        "market_id": 1,
                        "symbol": "BTC",
                        "sign": -1,
                        "position": "0.00020",
                    }
                ]
            },
        )

    client = _make_client(monkeypatch, handler)
    client.account_index = 7
    position = _run_and_close(client, client.get_position("BTC"))

    assert position.signed_size == Decimal("-0.00020")


def test_sign_one_produces_positive_signed_size(monkeypatch) -> None:
    """sign=1 必须解析为多头。"""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "code": 200,
                "positions": [
                    {"market_id": 1, "symbol": "BTC", "sign": 1, "position": "0.25"}
                ]
            },
        )

    client = _make_client(monkeypatch, handler)
    client.account_index = 7

    position = _run_and_close(client, client.get_position("BTC"))

    assert position.signed_size == Decimal("0.25")


def test_zero_position_entry_is_flat(monkeypatch) -> None:
    """响应中的零数量条目必须视为空仓。"""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "code": 200,
                "positions": [
                    {"market_id": 1, "symbol": "BTC", "sign": -1, "position": "0.00"}
                ]
            },
        )

    client = _make_client(monkeypatch, handler)
    client.account_index = 7

    position = _run_and_close(client, client.get_position("BTC"))

    assert position.signed_size == Decimal(0)


def test_missing_symbol_is_flat_without_error(monkeypatch) -> None:
    """成功响应里没有目标 symbol 时返回零仓位，不把它当读取故障。"""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "code": 200,
                "positions": [
                    {"market_id": 0, "symbol": "ETH", "sign": 1, "position": "1.5"}
                ]
            },
        )

    client = _make_client(monkeypatch, handler)
    client.account_index = 7

    position = _run_and_close(client, client.get_position("BTC"))

    assert position.market == "BTC"
    assert position.signed_size == Decimal(0)
    assert position.raw is None


@pytest.mark.parametrize(
    "positions",
    [
        [None],
        [{"symbol": "BTC", "sign": 0, "position": "0.20"}],
        [{"symbol": "BTC", "sign": 1, "position": "-0.20"}],
    ],
)
def test_malformed_position_data_raises_instead_of_returning_flat(
    monkeypatch,
    positions: list,
) -> None:
    """畸形元素、非零仓非法 sign、负数量都不得伪装成空仓。"""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 200, "positions": positions})

    client = _make_client(monkeypatch, handler)
    client.account_index = 7

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
    client.account_index = 7

    with pytest.raises(expected_exception):
        _run_and_close(client, client.get_position("BTC"))


def test_connect_resolves_and_caches_account_index(monkeypatch) -> None:
    """连接时应按 L1 地址反查并缓存账户索引。"""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/accountsByL1Address"
        assert request.url.params["l1_address"] == "0xabc"
        return httpx.Response(
            200,
            json={"code": 200, "accounts": [{"account_index": 23}]},
        )

    client = _make_client(monkeypatch, handler)

    _run_and_close(client, client.connect())

    assert client.account_index == 23


def test_connect_reports_unopened_address(monkeypatch) -> None:
    """code=21100 必须变成可读的地址未开户错误。"""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 21100, "message": "account not found"})

    client = _make_client(monkeypatch, handler)

    with pytest.raises(RuntimeError, match="地址未开户"):
        _run_and_close(client, client.connect())


@pytest.mark.parametrize("data", [{}, {"code": None}, {"code": 0}])
def test_legacy_success_codes_remain_accepted(data: dict) -> None:
    """缺失 code、None 和旧成功码 0 仍须保持兼容。"""
    LighterClient._raise_api_error(data, context="兼容性检查")


def test_lighter_client_declares_read_only_capability(monkeypatch) -> None:
    """真实 Lighter 适配器必须向引擎声明禁止自动改仓。"""
    client = _make_client(
        monkeypatch,
        lambda _request: httpx.Response(200, json={"code": 200}),
    )

    try:
        assert client.supports_trading is False
    finally:
        asyncio.run(client.close())


def test_market_order_is_intentionally_unsupported(monkeypatch) -> None:
    """人工腿不允许机器人下市价单。"""
    client = _make_client(
        monkeypatch,
        lambda _request: httpx.Response(200, json={"code": 200}),
    )

    with pytest.raises(NotImplementedError, match="人工"):
        _run_and_close(
            client,
            client.market_order("BTC", Side.BUY, Decimal("0.1")),
        )


def test_close_position_is_intentionally_unsupported(monkeypatch) -> None:
    """人工腿不允许机器人自动平仓。"""
    client = _make_client(
        monkeypatch,
        lambda _request: httpx.Response(200, json={"code": 200}),
    )

    with pytest.raises(NotImplementedError, match="人工"):
        _run_and_close(client, client.close_position("BTC"))


def test_market_price_uses_mark_price(monkeypatch) -> None:
    """订单簿详情没有 bid/ask 时，应以标记价构造统一行情。"""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/orderBookDetails"
        return httpx.Response(
            200,
            json={
                "code": 200,
                "order_book_details": [
                    {"market_id": 1, "symbol": "BTC", "mark_price": "62903.125"}
                ]
            },
        )

    client = _make_client(monkeypatch, handler)

    price = _run_and_close(client, client.get_market_price("BTC"))

    assert price.market == "BTC"
    assert price.bid == Decimal("62903.125")
    assert price.ask == Decimal("62903.125")
    assert price.mid == Decimal("62903.125")


def test_liquidation_info_uses_position_and_mark_price(monkeypatch) -> None:
    """有仓位且清算价非零时返回标记价与清算价。"""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/account":
            return httpx.Response(
                200,
                json={
                    "code": 200,
                    "positions": [
                        {
                            "market_id": 1,
                            "symbol": "BTC",
                            "sign": -1,
                            "position": "0.2",
                            "liquidation_price": "66912.223",
                        }
                    ]
                },
            )
        return httpx.Response(
            200,
            json={
                "code": 200,
                "order_book_details": [
                    {"market_id": 1, "symbol": "BTC", "mark_price": "62903.125"}
                ]
            },
        )

    client = _make_client(monkeypatch, handler)
    client.account_index = 7

    info = _run_and_close(client, client.get_liquidation_info("BTC"))

    assert info == (Decimal("62903.125"), Decimal("66912.223"))


@pytest.mark.parametrize(
    "position",
    [
        {"symbol": "BTC", "sign": 1, "position": "0.00", "liquidation_price": "0"},
        {"symbol": "BTC", "sign": 1, "position": "0.20", "liquidation_price": "0"},
    ],
)
def test_liquidation_info_is_none_without_live_liquidation_price(
    monkeypatch,
    position: dict,
) -> None:
    """无仓或清算价为零时没有可用清算信息。"""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"code": 200, "positions": [position]},
        )

    client = _make_client(monkeypatch, handler)
    client.account_index = 7

    info = _run_and_close(client, client.get_liquidation_info("BTC"))

    assert info is None
