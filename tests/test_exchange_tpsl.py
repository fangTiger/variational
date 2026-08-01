"""适配器扩展契约测试：reduce_only 透传、只撤网格单保留TPSL。

真实 SDK 下单/撤单的交易所行为在 testnet 单独验证（见计划 Task 9 说明）。
本测试只锁定"过滤逻辑"这一可纯测的部分。
"""
from __future__ import annotations

import asyncio
from decimal import Decimal
from types import SimpleNamespace

from adapters.base import Position
from adapters.extended_client import (
    ExtendedClient,
    filter_grid_orders,  # 纯函数：从开放单里挑出该撤的网格单
)
from grid.grid_engine import GridConfig, GridEngine
from grid.grid_state import GridState
from grid.regime import GridMode


def _o(oid, reduce_only=False, otype="LIMIT"):
    return SimpleNamespace(id=oid, reduce_only=reduce_only, type=otype)


def test_filter_keeps_tpsl_and_reduce_only() -> None:
    orders = [_o("g1"), _o("g2"), _o("tp", otype="TPSL"),
              _o("ro", reduce_only=True)]
    to_cancel = filter_grid_orders(orders)
    ids = {getattr(o, "id") for o in to_cancel}
    assert ids == {"g1", "g2"}  # 只撤普通网格单，保留 TPSL 与 reduce_only


def test_position_stop_loss_rounds_trigger_and_execution_prices(monkeypatch) -> None:
    """整仓 TPSL 的触发价与滑点执行价都必须按市场 tick 取整。"""
    rounded_inputs = []
    placed_orders = []

    class TradingConfig:
        min_price_change = Decimal("0.1")

        def round_price(self, price):
            value = Decimal(str(price))
            rounded_inputs.append(value)
            return value.quantize(self.min_price_change)

    async def place_order(*, order):
        placed_orders.append(order)
        return SimpleNamespace(data=SimpleNamespace(id="tpsl-1"))

    rest_client = SimpleNamespace(
        stark_account=object(),
        config=SimpleNamespace(
            signing=SimpleNamespace(starknet_domain=object()),
            defaults=SimpleNamespace(market_price_slippage=Decimal("0.01")),
        ),
        orders=SimpleNamespace(place_order=place_order),
    )
    client = ExtendedClient(rest_client)
    client._markets = {
        "BTC-USD": SimpleNamespace(trading_config=TradingConfig()),
    }

    monkeypatch.setattr(
        "adapters.extended_client.get_price_with_slippage",
        lambda **kwargs: Decimal("87.654"),
    )
    monkeypatch.setattr(
        "adapters.extended_client.create_order_object",
        lambda **kwargs: kwargs,
    )

    rounded = asyncio.run(client.round_price("BTC-USD", Decimal("88.123")))
    tick = asyncio.run(client.get_price_tick_size("BTC-USD"))
    asyncio.run(
        client.place_position_stop_loss(
            "BTC-USD",
            signed_size=Decimal("0.01"),
            trigger_price=Decimal("88.123"),
        )
    )

    assert rounded == Decimal("88.1")
    assert tick == Decimal("0.1")
    assert rounded_inputs == [
        Decimal("88.123"),
        Decimal("88.123"),
        Decimal("87.654"),
    ]
    stop_loss = placed_orders[0]["stop_loss"]
    assert stop_loss.trigger_price == Decimal("88.1")
    assert stop_loss.price == Decimal("87.7")


def test_tpsl_blocks_new_risk_when_unconfirmed(tmp_path, monkeypatch) -> None:
    """TPSL 挂单失败时仍处理已有订单，但本轮不能铺新阶梯。"""

    class NoTpslExt:
        def __init__(self) -> None:
            self.tpsl_calls = []
            self.open_orders_calls = 0
            self.placed = []
            self._client = SimpleNamespace(info=self)

        async def get_position(self, market):
            return Position(market=market, signed_size=Decimal("0.01"))

        async def get_liquidation_info(self, market):
            return Decimal("100"), Decimal("80")

        async def get_candles_history(self, **kwargs):
            candles = [
                SimpleNamespace(
                    timestamp=str(i),
                    high=101.0,
                    low=99.0,
                    close=100.0,
                )
                for i in range(4)
            ]
            return SimpleNamespace(data=candles)

        async def get_market_statistics(self, **kwargs):
            return SimpleNamespace(data=SimpleNamespace(mark_price=100.0))

        async def place_position_stop_loss(
            self,
            market,
            signed_size,
            trigger_price,
        ):
            self.tpsl_calls.append((market, signed_size, trigger_price))
            raise RuntimeError("tpsl rejected")

        async def get_open_orders(self, market):
            self.open_orders_calls += 1
            return []

        async def get_orders_history(self, market, limit=100, **kwargs):
            return []

        async def place_limit_order(self, market, side, amount, price, **kwargs):
            self.placed.append((market, side, amount, price, kwargs))
            return SimpleNamespace(data=SimpleNamespace(id="unexpected", status="NEW"))

    ext = NoTpslExt()
    cfg = GridConfig(
        dry_run=False,
        trend_aware=True,
        exchange_tpsl=True,
        state_path=str(tmp_path / "s.json"),
    )
    eng = GridEngine(ext, cfg)
    eng._state = GridState(95.0, 105.0, False, None, False)
    monkeypatch.setattr(
        "grid.grid_engine.trend_gate",
        lambda *args, **kwargs: GridMode.NEUTRAL,
    )

    result = asyncio.run(eng.run_once())

    assert len(ext.tpsl_calls) == 1
    market, size, trigger = ext.tpsl_calls[0]
    assert (market, size) == (cfg.market, Decimal("0.01"))
    assert abs(trigger - Decimal("90.9091")) < Decimal("0.001")
    assert ext.open_orders_calls == 1  # _handle_fills 仍处理已有订单
    assert ext.placed == []            # _maintain_ladder 未铺新单
    assert result == "TPSL 未确认：仅处理已有订单"
