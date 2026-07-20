"""黄金同所基差对冲模块测试（全 mock，不触发真实交易接口）。"""

from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest

from adapters.base import Position, Side
from adapters.variational_client import VariationalClient


def test_instrument_defaults_match_existing_btc_descriptor() -> None:
    """默认 instrument 描述符必须保持 BTC 现有行为不变。"""
    assert VariationalClient._instrument("BTC") == {
        "funding_interval_s": 3600,
        "instrument_type": "perpetual_future",
        "settlement_asset": "USDC",
        "underlying": "BTC",
    }


def test_instrument_accepts_explicit_gold_params() -> None:
    """显式参数能构造 XAU 与 XAUT 的 4h funding instrument。"""
    assert VariationalClient._instrument(
        "XAU",
        instrument_type="perpetual_rwa_future",
        funding_interval_s=14400,
        kind="commodity",
    ) == {
        "funding_interval_s": 14400,
        "instrument_type": "perpetual_rwa_future",
        "settlement_asset": "USDC",
        "underlying": "XAU",
        "kind": "commodity",
    }
    assert VariationalClient._instrument(
        "XAUT",
        instrument_type="perpetual_future",
        funding_interval_s=14400,
    ) == {
        "funding_interval_s": 14400,
        "instrument_type": "perpetual_future",
        "settlement_asset": "USDC",
        "underlying": "XAUT",
    }


def test_rwa_instrument_carries_kind_but_default_omits_it() -> None:
    """回归：RWA 永续必须带 kind（缺失后端 400 missing field `kind`）；
    非 RWA/默认调用不得引入 kind 字段，保持 BTC/XAUT 行为不变。"""
    # 默认（BTC）与显式非 RWA（XAUT）都不带 kind
    assert "kind" not in VariationalClient._instrument("BTC")
    assert "kind" not in VariationalClient._instrument(
        "XAUT", instrument_type="perpetual_future", funding_interval_s=14400
    )
    # XAU RWA 带 kind=commodity（与前端构造一致）
    assert VariationalClient._instrument(
        "XAU",
        instrument_type="perpetual_rwa_future",
        funding_interval_s=14400,
        kind="commodity",
    )["kind"] == "commodity"


def test_xau_leg_forwards_kind_into_quote_body() -> None:
    """hedge_gold 的 XAU 腿必须把 kind 透传进报价体，否则实盘 400。"""
    from tools import hedge_gold

    assert hedge_gold.XAU_LEG.kind == "commodity"
    assert hedge_gold.XAUT_LEG.kind is None
    # XAUT instrument 标识的 funding_interval 恒为 3600，用 14400 会 400 unsupported instrument
    assert hedge_gold.XAUT_LEG.funding_interval_s == 3600

    client = object.__new__(VariationalClient)
    capture: dict[str, object] = {}

    async def fake_post(path: str, body: dict | None = None):
        capture["body"] = body
        return {"quote_id": "q", "mark_price": "4000", "qty_step": "0.001"}

    client._post = fake_post
    asyncio.run(hedge_gold._quote_xau_basis(client))
    assert capture["body"]["instrument"]["kind"] == "commodity"
    assert capture["body"]["instrument"]["instrument_type"] == "perpetual_rwa_future"


def test_quote_and_market_order_forward_instrument_params() -> None:
    """request_quote / market_order 要把合约类型与 funding 间隔透传到 instrument。"""
    quote_client = object.__new__(VariationalClient)
    quote_capture: dict[str, object] = {}

    async def fake_post(path: str, body: dict | None = None):
        quote_capture["path"] = path
        quote_capture["body"] = body
        return {"quote_id": "q-xaut"}

    quote_client._post = fake_post

    asyncio.run(
        quote_client.request_quote(
            "XAUT",
            "sell",
            Decimal("0.2"),
            instrument_type="perpetual_future",
            funding_interval_s=14400,
        )
    )

    assert quote_capture["path"] == "/quotes/indicative"
    assert quote_capture["body"] == {
        "instrument": {
            "funding_interval_s": 14400,
            "instrument_type": "perpetual_future",
            "settlement_asset": "USDC",
            "underlying": "XAUT",
        },
        "qty": "0.2",
        "side": "sell",
    }

    order_client = object.__new__(VariationalClient)
    order_client._max_slippage = 0.01
    order_capture: dict[str, object] = {}

    async def fake_request_quote(
        underlying: str,
        side: str,
        qty: Decimal,
        *,
        instrument_type: str = "perpetual_future",
        funding_interval_s: int = 3600,
        kind: str | None = None,
    ):
        order_capture["quote_args"] = (
            underlying,
            side,
            qty,
            instrument_type,
            funding_interval_s,
        )
        return {"quote_id": "q-xau"}

    async def fake_accept_quote(
        *,
        quote_id: str,
        side: str,
        max_slippage: float,
        is_reduce_only: bool,
    ):
        order_capture["accept_args"] = (
            quote_id,
            side,
            max_slippage,
            is_reduce_only,
        )
        return {"rfq_id": "rfq-xau"}

    order_client.request_quote = fake_request_quote
    order_client.accept_quote = fake_accept_quote

    asyncio.run(
        order_client.market_order(
            "XAU",
            Side.BUY,
            Decimal("0.2"),
            reduce_only=True,
            instrument_type="perpetual_rwa_future",
            funding_interval_s=14400,
        )
    )

    assert order_capture["quote_args"] == (
        "XAU",
        "buy",
        Decimal("0.2"),
        "perpetual_rwa_future",
        14400,
    )
    assert order_capture["accept_args"] == ("q-xau", "buy", 0.01, True)


def _position_client() -> VariationalClient:
    client = object.__new__(VariationalClient)

    async def fake_get_positions():
        return [
            {
                "position_info": {
                    "instrument": {
                        "underlying": "XAUT",
                        "instrument_type": "perpetual_future",
                    },
                    "qty": "-0.25",
                }
            },
            {
                "position_info": {
                    "instrument": {
                        "underlying": "XAU",
                        "instrument_type": "perpetual_rwa_future",
                    },
                    "qty": "0.25",
                }
            },
        ]

    client.get_positions = fake_get_positions
    return client


def test_get_position_exact_disambiguates_xau_and_xaut() -> None:
    """精确匹配时 XAU 不能误命中 XAUT，同时默认旧子串行为保持不变。"""
    client = _position_client()

    legacy = asyncio.run(client.get_position("XAU"))
    xau = asyncio.run(client.get_position("XAU", exact=True))
    xaut = asyncio.run(client.get_position("XAUT", exact=True))

    assert legacy.signed_size == Decimal("-0.25")
    assert xau.signed_size == Decimal("0.25")
    assert xaut.signed_size == Decimal("-0.25")


def test_gold_net_delta_uses_exact_positions() -> None:
    """黄金模块计算净 delta 时必须各取各腿精确仓位。"""
    from tools import hedge_gold

    xau_size, xaut_size, net = asyncio.run(hedge_gold._net_delta(_position_client()))

    assert xau_size == Decimal("0.25")
    assert xaut_size == Decimal("-0.25")
    assert net == Decimal("0")


class FakeGoldVariational:
    """记录黄金模块下单意图的假 Variational 客户端。"""

    def __init__(self, *, fail_second_leg: bool = False) -> None:
        self.fail_second_leg = fail_second_leg
        self.sizes = {"XAU": Decimal("0"), "XAUT": Decimal("0")}
        self.orders: list[tuple[str, Side, Decimal, bool, str, int]] = []
        self.position_requests: list[tuple[str, bool]] = []

    async def request_quote(
        self,
        underlying: str,
        side: str,
        qty: Decimal,
        *,
        instrument_type: str = "perpetual_future",
        funding_interval_s: int = 3600,
        kind: str | None = None,
    ):
        return {
            "quote_id": f"q-{underlying}-{side}",
            "mark_price": "4000",
            "qty_step": "0.001",
            "instrument_type": instrument_type,
            "funding_interval_s": funding_interval_s,
        }

    async def get_position(self, underlying: str, *, exact: bool = False) -> Position:
        self.position_requests.append((underlying, exact))
        assert exact is True
        return Position(underlying, self.sizes[underlying])

    async def market_order(
        self,
        market: str,
        side: Side,
        amount: Decimal,
        *,
        reduce_only: bool = False,
        instrument_type: str = "perpetual_future",
        funding_interval_s: int = 3600,
        kind: str | None = None,
    ):
        self.orders.append(
            (market, side, amount, reduce_only, instrument_type, funding_interval_s)
        )
        if (
            self.fail_second_leg
            and market == "XAU"
            and side is Side.BUY
            and not reduce_only
        ):
            raise RuntimeError("模拟第二腿失败")

        delta = amount if side is Side.BUY else -amount
        current = self.sizes[market]
        if reduce_only and current > 0 and side is Side.SELL:
            self.sizes[market] = max(Decimal("0"), current - amount)
        elif reduce_only and current < 0 and side is Side.BUY:
            self.sizes[market] = min(Decimal("0"), current + amount)
        else:
            self.sizes[market] = current + delta
        return {"filled": str(amount)}

    async def get_funding_rate(
        self, underlying: str = "BTC", instrument_type: str = "perpetual_future"
    ) -> Decimal:
        return Decimal("0") if underlying == "XAU" else Decimal("0.1095")

    async def get_points_summary(self):
        return {"total_points": "123.4", "rank": 56}


def test_open_orders_thin_leg_first_and_rolls_back_when_second_leg_fails() -> None:
    """先开 XAUT 空；若 XAU 多失败，必须 reduce_only 回滚 XAUT。"""
    from tools import hedge_gold

    var = FakeGoldVariational(fail_second_leg=True)

    with pytest.raises(SystemExit) as exc:
        asyncio.run(hedge_gold.cmd_open(var, Decimal("300")))

    assert "已回滚" in str(exc.value)
    assert var.orders == [
        ("XAUT", Side.SELL, Decimal("0.075"), False, "perpetual_future", 3600),
        ("XAU", Side.BUY, Decimal("0.075"), False, "perpetual_rwa_future", 14400),
        ("XAUT", Side.BUY, Decimal("0.075"), True, "perpetual_future", 3600),
    ]
    assert var.sizes["XAUT"] == Decimal("0")
    assert var.sizes["XAU"] == Decimal("0")


def test_open_direction_guard_never_builds_reverse_pair() -> None:
    """开仓方向只能是多 XAU + 空 XAUT，不能构造反向组合。"""
    from tools import hedge_gold

    var = FakeGoldVariational()

    asyncio.run(hedge_gold.cmd_open(var, Decimal("300")))

    opening_orders = [order for order in var.orders if not order[3]]
    assert opening_orders == [
        ("XAUT", Side.SELL, Decimal("0.075"), False, "perpetual_future", 3600),
        ("XAU", Side.BUY, Decimal("0.075"), False, "perpetual_rwa_future", 14400),
    ]
    assert not any(
        market == "XAU" and side is Side.SELL
        for market, side, _amount, reduce_only, _itype, _interval in opening_orders
        if not reduce_only
    )
    assert not any(
        market == "XAUT" and side is Side.BUY
        for market, side, _amount, reduce_only, _itype, _interval in opening_orders
        if not reduce_only
    )


def test_close_flattens_xaut_before_xau_with_reduce_only() -> None:
    """平仓先平薄腿 XAUT，再平 XAU，且两腿都必须 reduce_only。"""
    from tools import hedge_gold

    var = FakeGoldVariational()
    var.sizes["XAU"] = Decimal("0.075")
    var.sizes["XAUT"] = Decimal("-0.075")

    asyncio.run(hedge_gold.cmd_close(var))

    assert var.orders == [
        ("XAUT", Side.BUY, Decimal("0.075"), True, "perpetual_future", 3600),
        ("XAU", Side.SELL, Decimal("0.075"), True, "perpetual_rwa_future", 14400),
    ]
    assert var.sizes == {"XAU": Decimal("0"), "XAUT": Decimal("0")}
