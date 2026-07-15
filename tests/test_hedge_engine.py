"""对冲引擎冒烟测试：用假适配器验证核心逻辑（不接真实交易所）。"""

from __future__ import annotations

import asyncio
from decimal import Decimal

from adapters.base import ExchangeAdapter, MarketPrice, Position, Side
from engine.hedge_engine import HedgeConfig, HedgeEngine
from engine.risk import RiskAction, RiskManager


class FakeAdapter(ExchangeAdapter):
    """可控的假适配器，记录下单意图。"""

    def __init__(self, name: str, size: Decimal = Decimal(0)) -> None:
        self.name = name
        self._size = size
        self.orders: list[tuple] = []
        self.fail = False

    async def connect(self) -> None:
        pass

    async def get_market_price(self, market: str) -> MarketPrice:
        return MarketPrice(market, Decimal("100"), Decimal("101"))

    async def get_position(self, market: str) -> Position:
        if self.fail:
            raise RuntimeError("模拟断连")
        return Position(market, self._size)

    async def market_order(self, market, side: Side, amount, *, reduce_only=False):
        self.orders.append((market, side, amount, reduce_only))
        # 模拟成交后更新持仓
        delta = amount if side is Side.BUY else -amount
        self._size += delta
        return {"filled": str(amount)}

    async def close(self) -> None:
        pass


def test_rebalance_opens_hedge_when_flat() -> None:
    """primary 有多头、hedge 为空 → dry_run 下应识别需再平衡。"""
    primary = FakeAdapter("primary", size=Decimal("1"))
    hedge = FakeAdapter("hedge", size=Decimal("0"))
    engine = HedgeEngine(primary, hedge, HedgeConfig(dry_run=True))

    state = asyncio.run(engine.run_once())

    assert state.net_delta == Decimal("1")
    assert "再平衡" in state.action_taken
    assert hedge.orders == []  # dry_run 不真正下单


def test_rebalance_live_neutralizes_delta() -> None:
    """非 dry_run：hedge 腿应下单把净 delta 拉回 0。"""
    primary = FakeAdapter("primary", size=Decimal("1"))
    hedge = FakeAdapter("hedge", size=Decimal("0"))
    engine = HedgeEngine(primary, hedge, HedgeConfig(dry_run=False))

    asyncio.run(engine.run_once())

    assert len(hedge.orders) == 1
    market, side, amount, reduce_only = hedge.orders[0]
    assert side is Side.SELL and amount == Decimal("1") and reduce_only is False
    assert hedge._size == Decimal("-1")  # 对冲后净 delta = 0


def test_no_rebalance_within_threshold() -> None:
    """已对冲（净 delta≈0）→ 不动手。"""
    primary = FakeAdapter("primary", size=Decimal("1"))
    hedge = FakeAdapter("hedge", size=Decimal("-1"))
    engine = HedgeEngine(primary, hedge, HedgeConfig(dry_run=False))

    state = asyncio.run(engine.run_once())

    assert state.net_delta == Decimal("0")
    assert state.action_taken == "无需再平衡"
    assert hedge.orders == []


def test_single_leg_failure_triggers_flatten() -> None:
    """单腿断连 → 风控裁决 FLATTEN。"""
    primary = FakeAdapter("primary", size=Decimal("1"))
    hedge = FakeAdapter("hedge", size=Decimal("-1"))
    hedge.fail = True
    engine = HedgeEngine(primary, hedge, HedgeConfig(dry_run=True))

    state = asyncio.run(engine.run_once())

    assert "紧急平仓" in state.action_taken


def test_risk_manager_flatten_on_disconnect() -> None:
    """风控单元：单腿数据缺失应返回 FLATTEN。"""
    rm = RiskManager()
    a = rm.assess(
        primary_size=Decimal("1"), hedge_size=Decimal("-1"),
        primary_ok=True, hedge_ok=False,
    )
    assert a.action is RiskAction.FLATTEN


if __name__ == "__main__":
    test_rebalance_opens_hedge_when_flat()
    test_rebalance_live_neutralizes_delta()
    test_no_rebalance_within_threshold()
    test_single_leg_failure_triggers_flatten()
    test_risk_manager_flatten_on_disconnect()
    print("✅ 全部冒烟测试通过")
