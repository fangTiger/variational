"""FROZEN 状态下的 reduce-only 回收阶梯。"""
from __future__ import annotations

import asyncio
from decimal import Decimal
from types import SimpleNamespace

from adapters.base import Position, Side
from grid.grid_engine import GridConfig, GridEngine
from grid.grid_state import GridState
from grid.regime import GridMode


class RecoveryExt:
    def __init__(self, signed_size: Decimal, mark: float) -> None:
        self.signed_size = signed_size
        self.mark = mark
        self.open_orders: list = []
        self.history: list = []
        self.placed: list[dict] = []

        class Info:
            async def get_candles_history(inner_self, **kwargs):
                candles = [
                    SimpleNamespace(timestamp=str(i), high=mark + 1, low=mark - 1, close=mark)
                    for i in range(4)
                ]
                return SimpleNamespace(data=candles)

        self._client = SimpleNamespace(info=Info())

    async def get_position(self, market):
        return Position(
            market=market,
            signed_size=self.signed_size,
            raw=SimpleNamespace(mark_price=str(self.mark), liquidation_price="0"),
        )

    async def get_open_orders(self, market):
        return self.open_orders

    async def get_orders_history(self, market, limit=100, **kwargs):
        return self.history

    async def place_limit_order(self, market, side, amount, price, **kwargs):
        self.placed.append(
            {
                "side": side,
                "amount": Decimal(str(amount)),
                "price": Decimal(str(price)),
                "reduce_only": kwargs.get("reduce_only", False),
            }
        )
        oid = f"ro-{len(self.placed)}"
        return SimpleNamespace(data=SimpleNamespace(id=oid, status="NEW"))


def _engine(ext: RecoveryExt, tmp_path, *, frozen: bool, blocked_side: str | None):
    eng = GridEngine(
        ext,
        GridConfig(
            dry_run=False,
            trend_aware=True,
            exchange_tpsl=False,
            spacing_pct=0.02,
            unit_usd=10.0,
            levels_per_side=3,
            max_inventory_usd=10.0,
            state_path=str(tmp_path / "grid_state.json"),
        ),
    )
    eng._state = GridState(95.0, 105.0, frozen, blocked_side, False)
    return eng


def test_frozen_long_places_reduce_only_sells_above_mark(tmp_path, monkeypatch) -> None:
    """多头跌破下界冻结 BUY 后，只在上方挂 reduce-only SELL。"""
    ext = RecoveryExt(Decimal("0.3"), mark=90.0)
    eng = _engine(ext, tmp_path, frozen=True, blocked_side="BUY")
    monkeypatch.setattr("grid.grid_engine.trend_gate", lambda *args, **kwargs: GridMode.NEUTRAL)

    asyncio.run(eng.run_once())

    assert [order["side"] for order in ext.placed] == [Side.SELL, Side.SELL, Side.SELL]
    assert all(order["reduce_only"] is True for order in ext.placed)
    assert all(order["price"] > Decimal("90") for order in ext.placed)
    assert abs(sum(order["amount"] for order in ext.placed) - Decimal("0.3")) < Decimal("1e-24")


def test_frozen_short_places_reduce_only_buys_below_mark(tmp_path, monkeypatch) -> None:
    """空头涨破上界冻结 SELL 后，只在下方挂 reduce-only BUY。"""
    ext = RecoveryExt(Decimal("-0.3"), mark=110.0)
    eng = _engine(ext, tmp_path, frozen=True, blocked_side="SELL")
    monkeypatch.setattr("grid.grid_engine.trend_gate", lambda *args, **kwargs: GridMode.NEUTRAL)

    asyncio.run(eng.run_once())

    assert [order["side"] for order in ext.placed] == [Side.BUY, Side.BUY, Side.BUY]
    assert all(order["reduce_only"] is True for order in ext.placed)
    assert all(order["price"] < Decimal("110") for order in ext.placed)
    assert abs(sum(order["amount"] for order in ext.placed) - Decimal("0.3")) < Decimal("1e-24")


def test_reduce_only_fill_does_not_flip() -> None:
    """减仓单成交后只移除跟踪记录，不再翻出新开仓单。"""
    ext = RecoveryExt(Decimal("0.3"), mark=100.0)
    ext.history = [SimpleNamespace(id="ro-1", status="FILLED", filled_qty="0.1")]
    eng = GridEngine(ext, GridConfig(dry_run=False))
    eng._orders = {100: {"id": "ro-1", "side": Side.SELL, "reduce_only": True}}

    asyncio.run(eng._handle_fills(0.0))

    assert eng._orders == {}
    assert ext.placed == []


def test_frozen_flat_position_does_not_place_recovery(tmp_path, monkeypatch) -> None:
    """仓位为 0 时，FROZEN 也不挂新的减仓阶梯。"""
    ext = RecoveryExt(Decimal("0"), mark=90.0)
    eng = _engine(ext, tmp_path, frozen=True, blocked_side="BUY")
    monkeypatch.setattr("grid.grid_engine.trend_gate", lambda *args, **kwargs: GridMode.NEUTRAL)

    asyncio.run(eng.run_once())

    assert ext.placed == []


def test_active_state_does_not_place_reduce_only_recovery(tmp_path, monkeypatch) -> None:
    """ACTIVE 状态可以正常补格，但不得挂 reduce-only 减仓单。"""
    ext = RecoveryExt(Decimal("0.3"), mark=100.0)
    eng = _engine(ext, tmp_path, frozen=False, blocked_side=None)
    monkeypatch.setattr("grid.grid_engine.trend_gate", lambda *args, **kwargs: GridMode.NEUTRAL)

    asyncio.run(eng.run_once())

    assert ext.placed
    assert all(order["reduce_only"] is False for order in ext.placed)


def test_reduce_only_orders_do_not_block_band_recenter(tmp_path) -> None:
    """只剩 reduce-only 单时，空仓 FROZEN 允许重建 band。"""
    ext = RecoveryExt(Decimal("0"), mark=100.0)
    ext.open_orders = [SimpleNamespace(id="ro", reduce_only=True, type="LIMIT")]
    eng = GridEngine(
        ext,
        GridConfig(
            dry_run=False,
            trend_aware=True,
            recenter_bars=1,
            state_path=str(tmp_path / "grid_state.json"),
        ),
    )
    eng._state = GridState(95.0, 105.0, True, "BUY", False)

    asyncio.run(eng._advance_band(100.0, GridMode.NEUTRAL, signed_size=Decimal("0")))

    assert eng._state == GridState(96.0, 104.0, False, None, False)
