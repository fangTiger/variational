"""Variational 执行模型与数量元数据测试。

数量约束的结构取自前端打包代码原文：
``qty_limits.bid`` / ``qty_limits.ask`` 各自带 min_qty / max_qty / min_qty_tick，
且前端按方向取值（买读 bid、卖读 ask）。这里的夹具与之保持一致。
"""

from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest

from adapters.base import ExchangeAdapter, PositionPnl, Side
from adapters.variational_client import VariationalClient


def _client_with_quote(quote: object) -> tuple[VariationalClient, list[tuple]]:
    """构造只返回指定报价元数据的客户端，并记录真实询价调用。"""
    client = object.__new__(VariationalClient)
    client._quantity_limits = {}
    calls: list[tuple] = []

    async def request_quote(
        underlying: str,
        side: str,
        qty: Decimal,
        *,
        instrument_type: str = "perpetual_future",
        funding_interval_s: int = 3600,
        kind: str | None = None,
    ) -> object:
        calls.append((underlying, side, qty))
        return quote

    client.request_quote = request_quote
    return client, calls


#: 双侧对称的正常报价。
_SYMMETRIC = {
    "margin_requirements": {"initial_margin": "0.2"},
    "qty_limits": {
        "bid": {"min_qty": "0.00003", "max_qty": "5", "min_qty_tick": "0.00001"},
        "ask": {"min_qty": "0.00003", "max_qty": "5", "min_qty_tick": "0.00001"},
    },
}

#: 双侧不对称——用于证明按方向取值，而不是把两侧合并。
_ASYMMETRIC = {
    "qty_limits": {
        "bid": {"min_qty": "0.00002", "max_qty": "9", "min_qty_tick": "0.00001"},
        "ask": {"min_qty": "0.00007", "max_qty": "3", "min_qty_tick": "0.0001"},
    },
}


def test_execution_model_defaults_to_orderbook_and_variational_declares_rfq() -> None:
    """旧适配器默认走订单簿，Variational 明确声明 RFQ。"""
    assert ExchangeAdapter.execution_model == "orderbook"
    assert VariationalClient.execution_model == "rfq"


def test_equity_market_order_automatically_uses_rwa_and_caches_metadata() -> None:
    """股票标的自动采用 RWA 参数，重复下单不得重复读取标的元数据。"""
    client = object.__new__(VariationalClient)
    client._max_slippage = 0.01
    metadata_calls = 0
    quote_bodies: list[dict] = []

    async def get_supported_assets():
        nonlocal metadata_calls
        metadata_calls += 1
        # 用平台权威字段，而非 token_uri：SNDK 是真实上市公司，
        # 图标来自第三方数据商，按图标 URL 判断会漏掉它。
        return {
            market: [
                {
                    "asset": market,
                    "instrument_type": "perpetual_rwa_future",
                    "asset_class": "equity",
                    "token_uri": f"https://images.example.com/{market}.png",
                }
            ]
            for market in ("OPENAI", "ANTHROPIC", "SNDK")
        }

    async def post(path: str, body: dict | None = None):
        if path == "/quotes/indicative":
            quote_bodies.append(body)
            return {"quote_id": f"quote-{len(quote_bodies)}"}
        assert path == "/quotes/accept"
        return {"rfq_id": body["quote_id"]}

    client.get_supported_assets = get_supported_assets
    client._post = post

    for market in ("OPENAI", "ANTHROPIC", "SNDK", "SNDK"):
        asyncio.run(client.market_order(market, Side.BUY, Decimal("0.1")))

    assert metadata_calls == 1
    assert [body["instrument"]["underlying"] for body in quote_bodies] == [
        "OPENAI",
        "ANTHROPIC",
        "SNDK",
        "SNDK",
    ]
    assert all(
        body["instrument"]["instrument_type"] == "perpetual_rwa_future"
        and body["instrument"]["kind"] == "equity"
        for body in quote_bodies
    )


def test_non_equity_market_order_keeps_standard_perpetual_instrument() -> None:
    """普通标的和无法识别的元数据都保持现有普通永续参数。"""
    client = object.__new__(VariationalClient)
    client._max_slippage = 0.01
    quote_bodies: list[dict] = []

    async def get_supported_assets():
        return {
            "BTC": [
                {
                    "asset": "BTC",
                    "token_uri": "https://assets.example/crypto/BTC.svg",
                }
            ]
        }

    async def post(path: str, body: dict | None = None):
        if path == "/quotes/indicative":
            quote_bodies.append(body)
            return {"quote_id": "quote-btc"}
        return {"rfq_id": "rfq-btc"}

    client.get_supported_assets = get_supported_assets
    client._post = post

    asyncio.run(client.market_order("BTC", Side.SELL, Decimal("0.001")))
    asyncio.run(client.market_order("SOL", Side.BUY, Decimal("0.01")))

    assert [body["instrument"] for body in quote_bodies] == [
        {
            "funding_interval_s": 3600,
            "instrument_type": "perpetual_future",
            "settlement_asset": "USDC",
            "underlying": "BTC",
        },
        {
            "funding_interval_s": 3600,
            "instrument_type": "perpetual_future",
            "settlement_asset": "USDC",
            "underlying": "SOL",
        },
    ]


def test_get_balance_reads_portfolio_balance_and_upnl_as_decimal() -> None:
    """Variational 权益必须由 portfolio 的余额与未实现盈亏精确相加。"""
    client = object.__new__(VariationalClient)

    async def fake_get(path: str):
        assert path == "/portfolio"
        return {
            "balance": "561.490000000000000001",
            "upnl": "-1.230000000000000002",
        }

    client._get = fake_get

    balance = asyncio.run(client.get_balance())

    assert balance.balance == Decimal("561.490000000000000001")
    assert balance.upnl == Decimal("-1.230000000000000002")
    assert balance.equity == Decimal("560.259999999999999999")


def test_get_position_pnl_uses_upnl_entry_and_mark_value() -> None:
    """Variational 名义价值由绝对数量乘标记价计算，空头不能得到负价值。"""
    client = object.__new__(VariationalClient)

    async def get_positions():
        return [
            {
                "position_info": {
                    "instrument": {"underlying": "BTC"},
                    "qty": "-0.021761",
                    "avg_entry_price": "77299.3",
                },
                "upnl": "4.33",
                "price_info": {"underlying_price": "77199.25"},
            }
        ]

    client.get_positions = get_positions

    snapshot = asyncio.run(client.get_position_pnl("BTC"))

    assert snapshot == PositionPnl(
        unrealized_pnl=Decimal("4.33"),
        entry_price=Decimal("77299.3"),
        position_value=Decimal("0.021761") * Decimal("77199.25"),
    )


def test_get_min_order_size_uses_quote_quantity_limits() -> None:
    """最小量只能采用报价返回的明确数量限制。"""
    client, calls = _client_with_quote(_SYMMETRIC)

    minimum = asyncio.run(client.get_min_order_size("BTC"))

    assert minimum == Decimal("0.00003")
    assert calls == [("BTC", "buy", Decimal("0.0001"))]


def test_get_min_order_size_rejects_when_api_omits_quantity_limit() -> None:
    """API 未给最小量时必须失败关闭，不能编造零或默认值。

    下限缺失若被静默放过，会提交低于门槛的单并被交易所拒绝，
    而策略侧误判成「已下单」，最终留下单边裸仓。
    """
    client, _ = _client_with_quote({"margin_requirements": {"initial_margin": "0.2"}})

    with pytest.raises(RuntimeError, match="min_qty"):
        asyncio.run(client.get_min_order_size("BTC"))


def test_round_amount_aligns_down_using_quote_quantity_tick() -> None:
    """数量按步长向下对齐，避免放大目标仓位。"""
    client, _ = _client_with_quote(_SYMMETRIC)

    rounded = asyncio.run(client.round_amount("BTC", Decimal("0.000037")))

    assert rounded == Decimal("0.00003")


def test_round_amount_preserves_value_when_api_omits_quantity_tick() -> None:
    """步长未知时保持基类行为原样返回，不猜测精度。"""
    client, _ = _client_with_quote({"qty_limits": {"bid": {"min_qty": "0.00003"}}})

    rounded = asyncio.run(client.round_amount("BTC", Decimal("0.000037")))

    assert rounded == Decimal("0.000037")


# ---- 按方向取值：把 bid/ask 合并的实现必须跑不过下面这组 ----


def test_min_order_size_reads_bid_for_buy_and_ask_for_sell() -> None:
    """买单读 bid 侧、卖单读 ask 侧，与前端实现一致。

    若把两侧用 max() 合并，买卖都会得到 0.00007，买单侧就被抬到
    实际不需要的门槛，可能把本来合法的小额补单判成不可下单。
    """
    client, _ = _client_with_quote(_ASYMMETRIC)

    buy_min = asyncio.run(client.get_min_order_size("BTC", Side.BUY))
    sell_min = asyncio.run(client.get_min_order_size("BTC", Side.SELL))

    assert buy_min == Decimal("0.00002")
    assert sell_min == Decimal("0.00007")


def test_max_order_size_reads_matching_side() -> None:
    """单笔上限同样按方向取；这条决定大额单能不能下进去。"""
    client, _ = _client_with_quote(_ASYMMETRIC)

    assert asyncio.run(client.get_max_order_size("BTC", Side.BUY)) == Decimal("9")
    assert asyncio.run(client.get_max_order_size("BTC", Side.SELL)) == Decimal("3")


def test_round_amount_uses_side_specific_tick() -> None:
    """步长按方向取：两侧精度不同时不能串用。"""
    client, _ = _client_with_quote(_ASYMMETRIC)

    buy = asyncio.run(client.round_amount("BTC", Decimal("0.00012345"), Side.BUY))
    sell = asyncio.run(client.round_amount("BTC", Decimal("0.00012345"), Side.SELL))

    assert buy == Decimal("0.00012")
    assert sell == Decimal("0.0001")


def test_side_omitted_falls_back_to_strictest_limits() -> None:
    """不传方向时取更严格的组合：下限取大、步长取粗、上限取小。

    基类契约是单参数 (market)，timed_volume 现在仍按单参数调用，
    此时无法判断方向，只能保守——宁可少下也不要超限。
    """
    client, _ = _client_with_quote(_ASYMMETRIC)

    assert asyncio.run(client.get_min_order_size("BTC")) == Decimal("0.00007")
    assert asyncio.run(client.get_max_order_size("BTC")) == Decimal("3")
    assert asyncio.run(client.round_amount("BTC", Decimal("0.00012345"))) == Decimal(
        "0.0001"
    )


def test_max_order_size_returns_none_when_absent() -> None:
    """上限缺失只是没有约束，不能像下限那样抛异常。"""
    client, _ = _client_with_quote(
        {"qty_limits": {"bid": {"min_qty": "0.00003", "min_qty_tick": "0.00001"}}}
    )

    assert asyncio.run(client.get_max_order_size("BTC", Side.BUY)) is None


def test_quantity_limits_cached_per_market_and_side() -> None:
    """询价是真实 API 调用，同一 (market, side) 只能打一次。"""
    client, calls = _client_with_quote(_ASYMMETRIC)

    asyncio.run(client.get_min_order_size("BTC", Side.BUY))
    asyncio.run(client.get_max_order_size("BTC", Side.BUY))
    asyncio.run(client.round_amount("BTC", Decimal("1"), Side.BUY))

    assert len(calls) == 1

    asyncio.run(client.get_min_order_size("BTC", Side.SELL))

    assert len(calls) == 2


def test_instrument_kind_comes_from_metadata_not_icon_url() -> None:
    """合约类型必须读平台标注的字段，不能从图标 URL 之类的展示字段推断。

    早期实现按 ``token_uri`` 是否含 ``/equities/`` 判断，对 SNDK 失效——
    它是真实上市公司，图标来自第三方金融数据商
    （images.financialmodelingprep.com），而 OPENAI/ANTHROPIC 未上市、
    只能用自家图标。结果 SNDK 被判成普通永续，下单直接报
    `unsupported instrument: P-SNDK-USDC-3600`。

    这条用「图标 URL 明确不含 equities，但元数据标了 RWA」的夹具，
    确保判定只看权威字段。
    """
    client = object.__new__(VariationalClient)

    async def get_supported_assets():
        return {
            "SNDK": [
                {
                    "asset": "SNDK",
                    "instrument_type": "perpetual_rwa_future",
                    "asset_class": "equity",
                    # 第三方图床，不含 /equities/
                    "token_uri": "https://images.financialmodelingprep.com/symbol/SNDK.png",
                }
            ],
            "XAU": [
                {
                    "asset": "XAU",
                    "instrument_type": "perpetual_rwa_future",
                    "asset_class": "commodity",
                }
            ],
            "BTC": [{"asset": "BTC", "instrument_type": "perpetual_future"}],
        }

    client.get_supported_assets = get_supported_assets

    assert asyncio.run(client._instrument_kind_from_metadata("SNDK")) == (
        "perpetual_rwa_future",
        "equity",
    )
    # 黄金的 kind 是 commodity，不是写死的 equity
    assert asyncio.run(client._instrument_kind_from_metadata("XAU")) == (
        "perpetual_rwa_future",
        "commodity",
    )
    assert asyncio.run(client._instrument_kind_from_metadata("BTC")) == (
        "perpetual_future",
        None,
    )


def test_instrument_kind_falls_back_when_metadata_unavailable() -> None:
    """元数据取不到时保持普通永续行为，不猜测。"""
    client = object.__new__(VariationalClient)

    async def get_supported_assets():
        raise RuntimeError("元数据服务不可用")

    client.get_supported_assets = get_supported_assets

    assert asyncio.run(client._instrument_kind_from_metadata("SNDK")) is None
