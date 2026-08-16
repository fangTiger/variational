"""对冲引擎 primary 名义上限与通用自愈异常测试。"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from adapters.base import ExchangeAdapter, MarketPrice, Position, Side
from engine.hedge_engine import HedgeConfig, HedgeEngine


class FakeAdapter(ExchangeAdapter):
    """提供固定行情和仓位，并用 AsyncMock 记录下单。"""

    def __init__(
        self,
        name: str,
        *,
        size: Decimal,
        mark_price: Decimal = Decimal("100"),
        position_error: Exception | None = None,
        fail_on_price_read: bool = False,
        liquidation_info: tuple[Decimal, Decimal] | None = None,
        free_margin: Decimal | None = None,
    ) -> None:
        self.name = name
        self.size = size
        self.mark_price = mark_price
        self.position_error = position_error
        self.fail_on_price_read = fail_on_price_read
        self.liquidation_info = liquidation_info
        self.free_margin = free_margin
        self.market_order = AsyncMock(side_effect=self._fill_order)

    async def connect(self) -> None:
        pass

    async def get_market_price(self, market: str) -> MarketPrice:
        if self.fail_on_price_read:
            raise AssertionError("未启用名义上限时不应额外读取行情")
        return MarketPrice(market=market, bid=self.mark_price, ask=self.mark_price)

    async def get_position(self, market: str) -> Position:
        if self.position_error is not None:
            raise self.position_error
        return Position(market=market, signed_size=self.size)

    async def get_liquidation_info(
        self,
        market: str,
    ) -> tuple[Decimal, Decimal] | None:
        del market
        return self.liquidation_info

    async def get_free_margin_ratio(self) -> Decimal | None:
        return self.free_margin

    async def market_order(
        self,
        market: str,
        side: Side,
        amount: Decimal,
        *,
        reduce_only: bool = False,
    ):
        raise AssertionError("实例初始化时会替换为 AsyncMock")

    async def _fill_order(
        self,
        market: str,
        side: Side,
        amount: Decimal,
        *,
        reduce_only: bool = False,
    ) -> dict:
        del market, reduce_only
        self.size += amount if side is Side.BUY else -amount
        return {"filled": str(amount)}

    async def close(self) -> None:
        pass


def test_primary_notional_above_cap_refuses_rebalance_without_order() -> None:
    """primary 超过名义上限时必须拒绝跟单，且下单次数严格为零。"""
    primary = FakeAdapter("primary", size=Decimal("2"), mark_price=Decimal("100"))
    hedge = FakeAdapter("hedge", size=Decimal("0"))
    config = HedgeConfig(dry_run=False, max_primary_notional=Decimal("150"))
    engine = HedgeEngine(primary, hedge, config)

    state = asyncio.run(engine.run_once())

    assert "名义金额 200" in state.action_taken
    assert "超过上限 150" in state.action_taken
    primary.market_order.assert_not_awaited()
    hedge.market_order.assert_not_awaited()


def test_primary_notional_above_cap_preserves_existing_hedge() -> None:
    """超限时已有对冲仓必须保持不动，不能平掉后制造裸仓。"""
    primary = FakeAdapter("primary", size=Decimal("2"), mark_price=Decimal("100"))
    hedge = FakeAdapter("hedge", size=Decimal("-1"))
    config = HedgeConfig(dry_run=False, max_primary_notional=Decimal("150"))
    engine = HedgeEngine(primary, hedge, config)

    state = asyncio.run(engine.run_once())

    assert "超过上限" in state.action_taken
    assert hedge.size == Decimal("-1")
    primary.market_order.assert_not_awaited()
    hedge.market_order.assert_not_awaited()


@pytest.mark.parametrize(
    ("risk_mode", "expected_alert"),
    [("liquidation", "逼近清仓价"), ("margin", "可用保证金率")],
)
def test_risk_alerts_precede_notional_cap_for_read_only_primary(
    risk_mode: str,
    expected_alert: str,
) -> None:
    """只读 primary 超限时仍须先发出风险告警，且两腿完全不下单。"""
    primary_kwargs = {}
    hedge_kwargs = {}
    config_kwargs = {}
    if risk_mode == "liquidation":
        primary_kwargs["liquidation_info"] = (Decimal("100"), Decimal("95"))
    else:
        hedge_kwargs["free_margin"] = Decimal("0.01")
        config_kwargs["min_free_margin_ratio"] = Decimal("0.10")

    primary = FakeAdapter(
        "primary",
        size=Decimal("2"),
        mark_price=Decimal("100"),
        **primary_kwargs,
    )
    primary.supports_trading = False
    hedge = FakeAdapter("hedge", size=Decimal("-1"), **hedge_kwargs)
    config = HedgeConfig(
        dry_run=False,
        max_primary_notional=Decimal("150"),
        **config_kwargs,
    )
    engine = HedgeEngine(primary, hedge, config)

    state = asyncio.run(engine.run_once())

    assert expected_alert in state.action_taken
    assert "primary 只读" in state.action_taken
    assert primary.size == Decimal("2")
    assert hedge.size == Decimal("-1")
    primary.market_order.assert_not_awaited()
    hedge.market_order.assert_not_awaited()


@pytest.mark.parametrize("risk_mode", ["liquidation", "margin"])
def test_read_only_primary_keeps_both_legs_unchanged_on_risk(risk_mode: str) -> None:
    """primary 只读时，双平/双减仓必须转为人工告警，不能只操作 hedge。"""
    primary_kwargs = {}
    hedge_kwargs = {}
    config_kwargs = {}
    if risk_mode == "liquidation":
        primary_kwargs["liquidation_info"] = (Decimal("100"), Decimal("95"))
    else:
        hedge_kwargs["free_margin"] = Decimal("0.01")
        config_kwargs["min_free_margin_ratio"] = Decimal("0.10")

    primary = FakeAdapter("primary", size=Decimal("1"), **primary_kwargs)
    primary.supports_trading = False
    hedge = FakeAdapter("hedge", size=Decimal("-1"), **hedge_kwargs)
    config = HedgeConfig(dry_run=False, **config_kwargs)
    engine = HedgeEngine(primary, hedge, config)

    state = asyncio.run(engine.run_once())

    assert "primary 只读" in state.action_taken
    assert "人工" in state.action_taken
    assert primary.size == Decimal("1")
    assert hedge.size == Decimal("-1")
    primary.market_order.assert_not_awaited()
    hedge.market_order.assert_not_awaited()


def test_primary_notional_equal_to_cap_allows_rebalance() -> None:
    """名义金额恰好等于上限时不能误伤，应正常建立对冲仓。"""
    primary = FakeAdapter("primary", size=Decimal("1"), mark_price=Decimal("100"))
    hedge = FakeAdapter("hedge", size=Decimal("0"))
    config = HedgeConfig(dry_run=False, max_primary_notional=Decimal("100"))
    engine = HedgeEngine(primary, hedge, config)

    state = asyncio.run(engine.run_once())

    assert state.action_taken == "再平衡 hedge → -1"
    hedge.market_order.assert_awaited_once_with(
        "BTC-USD",
        Side.SELL,
        Decimal("1"),
        reduce_only=False,
    )
    assert hedge.size == Decimal("-1")


def test_none_notional_cap_keeps_existing_behavior() -> None:
    """上限为 None 时不读额外行情，维持原有无条件再平衡行为。"""
    primary = FakeAdapter(
        "primary",
        size=Decimal("2"),
        fail_on_price_read=True,
    )
    hedge = FakeAdapter("hedge", size=Decimal("0"))
    config = HedgeConfig(dry_run=False, max_primary_notional=None)
    engine = HedgeEngine(primary, hedge, config)

    state = asyncio.run(engine.run_once())

    assert state.action_taken == "再平衡 hedge → -2"
    hedge.market_order.assert_awaited_once()
    assert hedge.size == Decimal("-2")


def test_configured_auth_error_type_propagates_to_self_heal() -> None:
    """配置的 primary 认证异常应向上抛出，交给既有自愈流程。"""

    class PrimaryAuthExpired(Exception):
        pass

    primary = FakeAdapter(
        "primary",
        size=Decimal("0"),
        position_error=PrimaryAuthExpired("会话失效"),
    )
    hedge = FakeAdapter("hedge", size=Decimal("0"))
    config = HedgeConfig(
        dry_run=False,
        auth_error_types=(PrimaryAuthExpired,),
    )
    engine = HedgeEngine(primary, hedge, config)

    with pytest.raises(PrimaryAuthExpired, match="会话失效"):
        asyncio.run(engine.run_once())
