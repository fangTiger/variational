"""定时定量对冲策略测试。

测试只使用内存适配器，不连接任何交易所。测试桩分别锁定 Lighter 与
Extended 的真实方法签名，避免虚构生产适配器不存在的能力。
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
import json
import logging
from decimal import Decimal
from types import SimpleNamespace

import pytest

from adapters.base import MarketPrice, Position, PositionPnl, Side
from adapters.extended_client import ExtendedClient
from adapters.lighter_client import LighterClient
from adapters.variational_client import VariationalClient
from engine.hedge_engine import HedgeFillResult


class _AdapterCore:
    """共享的内存持仓与订单行为；公开方法由两种真实签名的子类提供。"""

    def __init__(
        self,
        name: str,
        *,
        maker_fill: Decimal = Decimal("1"),
        min_order_size: Decimal = Decimal("0.001"),
        rounded_amount: Decimal | None = None,
        min_order_size_error: Exception | None = None,
    ) -> None:
        self.name = name
        self.position = Decimal("0")
        self.maker_fill = Decimal(str(maker_fill))
        self.min_order_size = Decimal(str(min_order_size))
        self.rounded_amount = (
            Decimal(str(rounded_amount)) if rounded_amount is not None else None
        )
        self.min_order_size_error = min_order_size_error
        self.events: list[tuple] = []
        self._orders: dict[str, SimpleNamespace] = {}

    async def get_market_price(self, market: str):
        return MarketPrice(market, bid=Decimal("99"), ask=Decimal("101"))

    async def get_position(self, market: str):
        return Position(market, self.position)

    async def get_min_order_size(self, market: str):
        del market
        if self.min_order_size_error is not None:
            raise self.min_order_size_error
        return self.min_order_size

    async def round_amount(self, market: str, amount: Decimal):
        del market
        if self.rounded_amount is not None:
            return self.rounded_amount
        return Decimal(str(amount)).quantize(Decimal("0.001"))

    async def _place(
        self,
        market: str,
        side: Side,
        amount: Decimal,
        price: Decimal,
        *,
        post_only: bool,
        reduce_only: bool,
    ):
        order_id = f"{self.name}-{len(self._orders) + 1}"
        signed = amount if side is Side.BUY else -amount
        filled = amount * self.maker_fill
        self.position += signed * self.maker_fill
        self.events.append(
            ("limit", market, side, amount, price, post_only, reduce_only)
        )
        order = SimpleNamespace(
            id=order_id,
            filled_qty=filled,
            status="FILLED" if filled >= amount else "NEW",
        )
        self._orders[order_id] = order
        return SimpleNamespace(data=SimpleNamespace(id=order_id))

    async def get_order_by_id(self, market: str, order_id):
        del market
        return self._orders[order_id]

    async def cancel_order(self, market: str, order_id) -> None:
        self.events.append(("cancel", market, order_id))

    async def market_order(
        self,
        market: str,
        side: Side,
        amount: Decimal,
        *,
        reduce_only: bool = False,
    ):
        signed = amount if side is Side.BUY else -amount
        self.position += signed
        self.events.append(("market", market, side, amount, reduce_only))
        return SimpleNamespace(data=SimpleNamespace(id=f"market-{self.name}"))


class _LighterAdapter(_AdapterCore):
    """公开签名与 ``LighterClient`` 一致。"""

    async def place_limit_order(
        self,
        market: str,
        side: Side,
        amount: Decimal,
        price: Decimal,
        *,
        post_only: bool = True,
        reduce_only: bool = False,
    ):
        return await self._place(
            market,
            side,
            amount,
            price,
            post_only=post_only,
            reduce_only=reduce_only,
        )


class _ExtendedAdapter(_AdapterCore):
    """公开签名与 ``ExtendedClient`` 一致。"""

    async def get_market_price(self, market_name: str):
        return MarketPrice(market_name, bid=Decimal("99"), ask=Decimal("101"))

    async def get_position(self, market_name: str):
        return Position(market_name, self.position)

    async def place_limit_order(
        self,
        market: str,
        side: Side,
        amount: Decimal,
        price: Decimal,
        *,
        post_only: bool = True,
        expire_days: int = 90,
        reduce_only: bool = False,
    ):
        del expire_days
        return await self._place(
            market,
            side,
            amount,
            price,
            post_only=post_only,
            reduce_only=reduce_only,
        )


class _RfqAdapter:
    """只实现 RFQ 公共能力，不伪造订单簿方法。"""

    execution_model = "rfq"

    def __init__(
        self,
        name: str = "rfq",
        *,
        min_order_size: Decimal = Decimal("0.001"),
    ) -> None:
        self.name = name
        self.position = Decimal("0")
        self.min_order_size = Decimal(str(min_order_size))
        self.events: list[tuple] = []

    async def get_market_price(self, market: str):
        self.events.append(("quote", market))
        return MarketPrice(market, bid=Decimal("99"), ask=Decimal("101"))

    async def get_position(self, market: str):
        return Position(market, self.position)

    async def get_min_order_size(self, market: str):
        del market
        return self.min_order_size

    async def round_amount(self, market: str, amount: Decimal):
        del market
        return Decimal(str(amount)).quantize(Decimal("0.001"))

    async def market_order(
        self,
        market: str,
        side: Side,
        amount: Decimal,
        *,
        reduce_only: bool = False,
    ):
        signed = amount if side is Side.BUY else -amount
        self.position += signed
        self.events.append(("market", market, side, amount, reduce_only))
        return SimpleNamespace(data=SimpleNamespace(id=f"market-{self.name}"))


class _ScriptedExecutor:
    """按腿模拟全成、部分成交和成交后异常，并记录策略给出的实际差额。"""

    def __init__(self, plans: dict[str, list[dict]] | None = None) -> None:
        self.plans = {name: list(items) for name, items in (plans or {}).items()}
        self.calls: list[dict] = []

    async def __call__(
        self,
        adapter,
        market: str,
        target_delta: Decimal,
        *,
        timeout_s: float,
        poll_s: float,
        reduce_only: bool,
    ) -> HedgeFillResult:
        plan = (
            self.plans.get(adapter.name, []).pop(0)
            if self.plans.get(adapter.name)
            else {}
        )
        fraction = Decimal(str(plan.get("fraction", "1")))
        actual_delta = Decimal(
            str(plan.get("actual_delta", target_delta * fraction))
        )
        adapter.position += actual_delta
        self.calls.append(
            {
                "adapter": adapter.name,
                "market": market,
                "target_delta": target_delta,
                "actual_delta": actual_delta,
                "timeout_s": timeout_s,
                "poll_s": poll_s,
                "reduce_only": reduce_only,
            }
        )
        if error := plan.get("error"):
            if isinstance(error, Exception):
                raise error
            raise RuntimeError(str(error))
        reported = Decimal(str(plan.get("reported_fill", abs(target_delta))))
        return HedgeFillResult(filled=reported, used_taker=False)


def _signature_shape(method):
    """只比较调用契约，不要求测试桩复制生产类型注解。"""
    return [
        (parameter.name, parameter.kind, parameter.default)
        for parameter in inspect.signature(method).parameters.values()
    ]


@pytest.mark.parametrize(
    ("fake_type", "real_type", "method_name"),
    [
        (_LighterAdapter, LighterClient, "get_market_price"),
        (_LighterAdapter, LighterClient, "get_position"),
        (_LighterAdapter, LighterClient, "get_min_order_size"),
        (_LighterAdapter, LighterClient, "round_amount"),
        (_LighterAdapter, LighterClient, "place_limit_order"),
        (_LighterAdapter, LighterClient, "get_order_by_id"),
        (_LighterAdapter, LighterClient, "cancel_order"),
        (_LighterAdapter, LighterClient, "market_order"),
        (_ExtendedAdapter, ExtendedClient, "get_market_price"),
        (_ExtendedAdapter, ExtendedClient, "get_position"),
        (_ExtendedAdapter, ExtendedClient, "get_min_order_size"),
        (_ExtendedAdapter, ExtendedClient, "round_amount"),
        (_ExtendedAdapter, ExtendedClient, "place_limit_order"),
        (_ExtendedAdapter, ExtendedClient, "get_order_by_id"),
        (_ExtendedAdapter, ExtendedClient, "cancel_order"),
        (_ExtendedAdapter, ExtendedClient, "market_order"),
    ],
)
def test_adapter_stubs_match_real_signatures(fake_type, real_type, method_name) -> None:
    assert _signature_shape(getattr(fake_type, method_name)) == _signature_shape(
        getattr(real_type, method_name)
    )


def _api():
    """延迟导入，让 13 条规格测试分别因策略模块缺失而失败。"""
    return importlib.import_module("timed_volume.strategy")


def _build_strategy(
    tmp_path,
    *,
    lighter: _LighterAdapter | None = None,
    extended: _ExtendedAdapter | None = None,
    executor=None,
    hedge_available=None,
    state_name: str = "state.json",
    initial_direction: str = "long",
    maker_timeout_s: float = 300.0,
    position_tolerance: Decimal = Decimal("0.000001"),
    auth_error_types: tuple[type[Exception], ...] = (),
    on_auth_error=None,
):
    api = _api()
    lighter = lighter or _LighterAdapter("lighter")
    extended = extended or _ExtendedAdapter("extended")
    config = api.TimedVolumeConfig(
        primary_market="BTC",
        hedge_market="BTC-USD",
        notional_min_usd=2000,
        notional_max_usd=2000,
        cycle_seconds=7200.0,
        initial_direction=api.RoundDirection(initial_direction),
        maker_timeout_s=maker_timeout_s,
        maker_poll_s=0.0,
        position_tolerance=position_tolerance,
        state_path=tmp_path / state_name,
    )
    strategy = api.TimedHedgedVolumeStrategy(
        lighter,
        extended,
        config,
        trade_executor=executor,
        hedge_available=hedge_available,
        auth_error_types=auth_error_types,
        on_auth_error=on_auth_error,
    )
    return api, strategy, lighter, extended


def test_primary_auth_error_reloads_client_and_skips_round(tmp_path) -> None:
    """3.1/3.2：主腿认证失效后重建客户端，本轮不得下单。"""

    class TestAuthError(Exception):
        """测试专用认证异常。"""

    class ExpiredPrimary(_LighterAdapter):
        async def get_position(self, market: str):
            del market
            raise TestAuthError("Cookie 已过期")

    replacement = _LighterAdapter("lighter")
    reload_calls = []

    def reload_primary():
        reload_calls.append(True)
        return replacement

    executor = _ScriptedExecutor()
    _, strategy, _, extended = _build_strategy(
        tmp_path,
        lighter=ExpiredPrimary("lighter"),
        extended=_ExtendedAdapter("extended"),
        executor=executor,
        auth_error_types=(TestAuthError,),
        on_auth_error=reload_primary,
    )

    result = asyncio.run(strategy.run_once(now=0.0))

    assert result.action == "auth_reloaded"
    assert reload_calls == [True]
    assert strategy.primary is replacement
    assert executor.calls == []
    assert extended.position == 0


def test_primary_auth_reload_still_failing_interlocks_open(tmp_path) -> None:
    """3.3：重建后的主腿仍认证失败时必须互锁并告警。"""

    class TestAuthError(Exception):
        """测试专用认证异常。"""

    class ExpiredPrimary(_LighterAdapter):
        async def get_position(self, market: str):
            del market
            raise TestAuthError("Cookie 已过期")

    async def reload_primary():
        return ExpiredPrimary("lighter")

    executor = _ScriptedExecutor()
    _, strategy, _, _ = _build_strategy(
        tmp_path,
        lighter=ExpiredPrimary("lighter"),
        executor=executor,
        auth_error_types=(TestAuthError,),
        on_auth_error=reload_primary,
    )

    result = asyncio.run(strategy.run_once(now=0.0))

    assert result.action == "auth_reload_failed"
    assert result.hedge_available is False
    assert "认证重载失败" in result.interlock_reason
    assert any("认证重载失败" in warning for warning in result.warnings)
    assert executor.calls == []


def test_primary_auth_error_from_minimum_query_also_reloads(tmp_path) -> None:
    """3.2：主腿元数据查询的认证异常不得被包装成普通互锁。"""

    class TestAuthError(Exception):
        """测试专用认证异常。"""

    primary = _LighterAdapter(
        "lighter",
        min_order_size_error=TestAuthError("数量约束认证失效"),
    )
    replacement = _LighterAdapter("lighter")
    _, strategy, _, _ = _build_strategy(
        tmp_path,
        lighter=primary,
        executor=_ScriptedExecutor(),
        auth_error_types=(TestAuthError,),
        on_auth_error=lambda: replacement,
    )

    result = asyncio.run(strategy.run_once(now=0.0))

    assert result.action == "auth_reloaded"
    assert strategy.primary is replacement
    assert "认证已重载" in result.interlock_reason


def test_auth_failure_after_other_leg_fill_rolls_back_filled_leg(tmp_path) -> None:
    """3.4：主腿认证拒绝而对冲腿成交时，即使重载失败也必须回滚对冲腿。"""

    class TestAuthError(Exception):
        """测试专用认证异常。"""

    primary = _LighterAdapter("lighter")
    hedge = _ExtendedAdapter("extended")
    executor = _ScriptedExecutor(
        {
            "lighter": [
                {"fraction": "0", "error": TestAuthError("下单认证失效")},
            ],
            "extended": [{}, {}],
        }
    )

    def failed_reload():
        raise TestAuthError("新 Cookie 仍失效")

    _, strategy, _, _ = _build_strategy(
        tmp_path,
        lighter=primary,
        extended=hedge,
        executor=executor,
        auth_error_types=(TestAuthError,),
        on_auth_error=failed_reload,
    )

    result = asyncio.run(strategy.run_once(now=0.0))

    assert result.action == "auth_reload_failed"
    assert primary.position == 0
    assert hedge.position == 0
    assert executor.calls[-1]["adapter"] == "extended"
    assert executor.calls[-1]["target_delta"] == Decimal("20.000")
    assert executor.calls[-1]["reduce_only"] is True


def test_position_before_due_is_not_closed(tmp_path) -> None:
    """1.1：持仓未满周期时不得平仓。"""
    executor = _ScriptedExecutor()
    _, strategy, lighter, extended = _build_strategy(tmp_path, executor=executor)
    asyncio.run(strategy.run_once(now=0.0))
    executor.calls.clear()

    result = asyncio.run(strategy.run_once(now=7199.0))

    assert result.action == "wait"
    assert executor.calls == []
    assert lighter.position == Decimal("20.000")
    assert extended.position == Decimal("-20.000")


def test_position_at_due_time_is_closed(tmp_path) -> None:
    """1.1：持仓达到周期时同步平掉两腿。"""
    executor = _ScriptedExecutor()
    _, strategy, lighter, extended = _build_strategy(tmp_path, executor=executor)
    asyncio.run(strategy.run_once(now=0.0))

    result = asyncio.run(strategy.run_once(now=7200.0))

    assert result.action == "closed"
    assert lighter.position == 0
    assert extended.position == 0
    assert [call["reduce_only"] for call in executor.calls[-2:]] == [True, True]


def test_existing_position_does_not_add_another_open(tmp_path) -> None:
    """1.2：周期内已有仓位时不追加开仓。"""
    executor = _ScriptedExecutor()
    _, strategy, _, _ = _build_strategy(tmp_path, executor=executor)
    asyncio.run(strategy.run_once(now=0.0))
    first_open_calls = len(executor.calls)

    asyncio.run(strategy.run_once(now=100.0))
    asyncio.run(strategy.run_once(now=200.0))

    assert len(executor.calls) == first_open_calls


def test_direction_alternates_and_restart_keeps_sequence(tmp_path) -> None:
    """1.3：平仓后的下一轮反向，重启后不得重复上一方向。"""
    executor = _ScriptedExecutor()
    api, strategy, lighter, extended = _build_strategy(tmp_path, executor=executor)
    asyncio.run(strategy.run_once(now=0.0))
    assert strategy.state.current_direction is api.RoundDirection.LONG
    asyncio.run(strategy.run_once(now=7200.0))
    assert lighter.position == 0
    assert extended.position == 0

    restarted_executor = _ScriptedExecutor()
    _, restarted, _, _ = _build_strategy(
        tmp_path,
        lighter=lighter,
        extended=extended,
        executor=restarted_executor,
    )
    asyncio.run(restarted.run_once(now=7200.0))

    assert restarted.state.current_direction is api.RoundDirection.SHORT
    assert restarted_executor.calls[0]["target_delta"] == Decimal("-20.000")


def test_open_is_delta_neutral_and_close_is_flat(tmp_path) -> None:
    """1.4：开仓后净敞口为零，平仓后两侧均为零。"""
    executor = _ScriptedExecutor()
    _, strategy, lighter, extended = _build_strategy(tmp_path, executor=executor)

    opened = asyncio.run(strategy.run_once(now=0.0))
    assert opened.net_exposure == 0
    assert lighter.position == -extended.position != 0

    closed = asyncio.run(strategy.run_once(now=7200.0))
    assert closed.net_exposure == 0
    assert lighter.position == 0
    assert extended.position == 0


def test_pnl_read_failure_does_not_block_open_or_activate_interlock(tmp_path) -> None:
    """展示用盈亏查询失败时仍须正常开仓，并在心跳里写入空值。"""

    class PnlFailingLighter(_LighterAdapter):
        async def get_position_pnl(self, market: str):
            del market
            raise RuntimeError("展示接口暂时不可用")

    executor = _ScriptedExecutor()
    _, strategy, lighter, extended = _build_strategy(
        tmp_path,
        lighter=PnlFailingLighter("lighter"),
        executor=executor,
    )

    result = asyncio.run(strategy.run_once(now=0.0))
    payload = importlib.import_module("tools.run_timed_volume").heartbeat_payload(
        result,
        now=1.0,
    )

    assert result.action == "opened"
    assert result.hedge_available is True
    assert lighter.position == -extended.position != 0
    assert payload["primary_pnl"] is None
    assert payload["hedge_pnl"] is None
    assert payload["primary_entry"] is None
    assert payload["hedge_entry"] is None
    assert payload["pair_pnl"] is None


def test_position_pnl_snapshots_are_combined_without_float_conversion(tmp_path) -> None:
    """两腿快照保留 Decimal 精度，且本对盈亏只做十进制加法。"""

    class PnlLighter(_LighterAdapter):
        async def get_position_pnl(self, market: str):
            del market
            return PositionPnl(
                unrealized_pnl=Decimal("4.330000000000000001"),
                entry_price=Decimal("77299.300000000000000001"),
                position_value=Decimal("1680.25"),
            )

    class PnlExtended(_ExtendedAdapter):
        async def get_position_pnl(self, market: str):
            del market
            return PositionPnl(
                unrealized_pnl=Decimal("-5.570000000000000002"),
                entry_price=Decimal("77301.1"),
                position_value=Decimal("1680.30"),
            )

    _, strategy, _, _ = _build_strategy(
        tmp_path,
        lighter=PnlLighter("lighter"),
        extended=PnlExtended("extended"),
        executor=_ScriptedExecutor(),
    )

    result = asyncio.run(strategy.run_once(now=0.0))

    assert result.primary_pnl == Decimal("4.330000000000000001")
    assert result.hedge_pnl == Decimal("-5.570000000000000002")
    assert result.primary_entry == Decimal("77299.300000000000000001")
    assert result.hedge_entry == Decimal("77301.1")
    assert result.pair_pnl == Decimal("-1.240000000000000001")


def test_lighter_opened_but_extended_failed_rolls_back_lighter(
    tmp_path,
    caplog,
) -> None:
    """1.5：Extended 未建仓时必须按实际持仓回滚 Lighter。"""
    executor = _ScriptedExecutor(
        {
            "lighter": [{}, {}],
            "extended": [{"fraction": "0", "error": "对冲超时"}],
        }
    )
    _, strategy, lighter, extended = _build_strategy(tmp_path, executor=executor)

    with caplog.at_level(logging.WARNING):
        result = asyncio.run(strategy.run_once(now=0.0))

    assert result.action == "open_failed_flat"
    assert lighter.position == 0
    assert extended.position == 0
    assert executor.calls[-1]["adapter"] == "lighter"
    assert executor.calls[-1]["target_delta"] == Decimal("-20.000")
    assert "回滚 Lighter" in caplog.text


def test_extended_opened_but_lighter_failed_closes_extended(
    tmp_path,
    caplog,
) -> None:
    """1.6：Lighter 未成交时必须平掉 Extended。"""
    executor = _ScriptedExecutor(
        {
            "lighter": [{"fraction": "0", "error": "开仓超时"}],
            "extended": [{}, {}],
        }
    )
    _, strategy, lighter, extended = _build_strategy(tmp_path, executor=executor)

    with caplog.at_level(logging.WARNING):
        result = asyncio.run(strategy.run_once(now=0.0))

    assert result.action == "open_failed_flat"
    assert lighter.position == 0
    assert extended.position == 0
    assert executor.calls[-1]["adapter"] == "extended"
    assert executor.calls[-1]["target_delta"] == Decimal("20.000")
    assert "平掉 Extended" in caplog.text


def test_partial_fill_uses_actual_position_difference(tmp_path) -> None:
    """1.7：部分成交的补单量来自两侧实仓差，不相信执行器回报的委托量。"""
    executor = _ScriptedExecutor(
        {
            "lighter": [
                {"fraction": "0.4", "reported_fill": "20"},
                {},
            ],
            "extended": [{}],
        }
    )
    _, strategy, lighter, extended = _build_strategy(tmp_path, executor=executor)

    result = asyncio.run(strategy.run_once(now=0.0))

    lighter_calls = [c for c in executor.calls if c["adapter"] == "lighter"]
    assert lighter_calls[1]["target_delta"] == Decimal("12.0000")
    assert result.net_exposure == 0
    assert lighter.position == Decimal("20.0000")
    assert extended.position == Decimal("-20.000")


def test_incident_rounding_gap_is_accepted_and_round_advances(tmp_path) -> None:
    """事故回放：跨所相差一个数量步长仍应在容差内完成轮次。"""
    lighter = _LighterAdapter(
        "lighter",
        min_order_size=Decimal("0.00020"),
        rounded_amount=Decimal("0.00129"),
    )
    extended = _ExtendedAdapter(
        "extended",
        min_order_size=Decimal("0.00010"),
        rounded_amount=Decimal("0.00128"),
    )
    executor = _ScriptedExecutor()
    _, strategy, _, _ = _build_strategy(
        tmp_path,
        lighter=lighter,
        extended=extended,
        executor=executor,
    )

    result = asyncio.run(strategy.run_once(now=0.0))

    assert result.action == "opened"
    assert result.action != "execution_uncertain"
    assert result.round_index == 1
    assert lighter.position == Decimal("0.00129")
    assert extended.position == Decimal("-0.00128")
    assert result.net_exposure == Decimal("0.00001")
    assert len(executor.calls) == 2

    replayed = asyncio.run(strategy.run_once(now=1.0))
    assert replayed.action == "wait"
    assert replayed.action != "execution_uncertain"
    assert len(executor.calls) == 2


def test_exposure_above_dynamic_tolerance_supplements_from_actual_positions(
    tmp_path,
) -> None:
    """净敞口超过动态容差时，必须按重读实仓补齐较小的一腿。"""
    lighter = _LighterAdapter(
        "lighter",
        min_order_size=Decimal("0.00020"),
    )
    extended = _ExtendedAdapter(
        "extended",
        min_order_size=Decimal("0.00010"),
    )
    lighter.position = Decimal("0.00129")
    extended.position = Decimal("-0.00098")
    executor = _ScriptedExecutor()
    _, strategy, _, _ = _build_strategy(
        tmp_path,
        lighter=lighter,
        extended=extended,
        executor=executor,
    )

    result = asyncio.run(strategy.run_once(now=100.0))

    assert result.action == "reconciled"
    assert len(executor.calls) == 1
    assert executor.calls[0]["adapter"] == "extended"
    assert executor.calls[0]["target_delta"] == Decimal("-0.00031")
    assert result.net_exposure == 0


def test_exposure_equal_to_dynamic_tolerance_still_supplements(tmp_path) -> None:
    """净敞口恰等于动态容差不算达成，必须继续按实仓补齐。"""
    lighter = _LighterAdapter(
        "lighter",
        min_order_size=Decimal("0.00010"),
    )
    extended = _ExtendedAdapter(
        "extended",
        min_order_size=Decimal("0.00010"),
    )
    lighter.position = Decimal("0.00100")
    extended.position = Decimal("-0.00080")
    executor = _ScriptedExecutor()
    _, strategy, _, _ = _build_strategy(
        tmp_path,
        lighter=lighter,
        extended=extended,
        executor=executor,
        position_tolerance=Decimal("0.00020"),
    )

    result = asyncio.run(strategy.run_once(now=100.0))

    assert result.action == "reconciled"
    assert len(executor.calls) == 1
    assert executor.calls[0]["adapter"] == "extended"
    assert executor.calls[0]["target_delta"] == Decimal("-0.00020")
    assert result.net_exposure == 0


def test_tradeable_neutral_legs_are_not_hidden_by_configured_tolerance(
    tmp_path,
) -> None:
    """两腿达到各自最小量时，即使小于配置容差也必须识别为真实对冲仓。"""
    lighter = _LighterAdapter(
        "lighter",
        min_order_size=Decimal("0.00010"),
    )
    extended = _ExtendedAdapter(
        "extended",
        min_order_size=Decimal("0.00010"),
    )
    lighter.position = Decimal("0.00015")
    extended.position = Decimal("-0.00015")
    executor = _ScriptedExecutor()
    _, strategy, _, _ = _build_strategy(
        tmp_path,
        lighter=lighter,
        extended=extended,
        executor=executor,
        position_tolerance=Decimal("0.00020"),
    )

    result = asyncio.run(strategy.run_once(now=100.0))

    assert result.action == "wait"
    assert result.round_index == 1
    assert result.primary_size == Decimal("0.00015")
    assert result.hedge_size == Decimal("-0.00015")
    assert executor.calls == []


def test_supplement_below_side_minimum_is_skipped_and_warned(
    tmp_path,
    caplog,
) -> None:
    """容差内缺口低于该侧最小下单量时，不得发送注定失败的补单。"""
    lighter = _LighterAdapter(
        "lighter",
        min_order_size=Decimal("0.00020"),
        rounded_amount=Decimal("0.00100"),
    )
    extended = _ExtendedAdapter(
        "extended",
        min_order_size=Decimal("0.00010"),
        rounded_amount=Decimal("0.00100"),
    )
    executor = _ScriptedExecutor(
        {"lighter": [{"actual_delta": "0.00095"}]}
    )
    _, strategy, _, _ = _build_strategy(
        tmp_path,
        lighter=lighter,
        extended=extended,
        executor=executor,
    )

    with caplog.at_level(logging.WARNING):
        result = asyncio.run(strategy.run_once(now=0.0))

    assert result.action == "opened"
    assert result.net_exposure == Decimal("-0.00005")
    assert len(executor.calls) == 2
    assert "补齐所需数量" in caplog.text
    assert "低于 Lighter 最小下单量" in caplog.text


def test_neutral_partial_fills_do_not_trigger_target_gap_supplements(
    tmp_path,
) -> None:
    """两腿部分成交但净敞口已在容差内时，不得再按各自目标缺口补单。"""
    lighter = _LighterAdapter(
        "lighter",
        min_order_size=Decimal("0.00020"),
        rounded_amount=Decimal("0.00100"),
    )
    extended = _ExtendedAdapter(
        "extended",
        min_order_size=Decimal("0.00010"),
        rounded_amount=Decimal("0.00100"),
    )
    executor = _ScriptedExecutor(
        {
            "lighter": [
                {"actual_delta": "0.00050"},
                {"fraction": "0"},
            ],
            "extended": [
                {"actual_delta": "-0.00050"},
                {"fraction": "0"},
            ],
        }
    )
    _, strategy, _, _ = _build_strategy(
        tmp_path,
        lighter=lighter,
        extended=extended,
        executor=executor,
    )

    result = asyncio.run(strategy.run_once(now=0.0))

    assert result.action == "opened"
    assert result.round_index == 1
    assert result.primary_size == Decimal("0.00050")
    assert result.hedge_size == Decimal("-0.00050")
    assert len(executor.calls) == 2


def test_flatten_dust_below_each_side_minimum_closes_without_retry(
    tmp_path,
    caplog,
) -> None:
    """平仓只剩不可交易微仓时应结束轮次，不得反复提交残差。"""
    lighter = _LighterAdapter(
        "lighter",
        min_order_size=Decimal("0.00020"),
        rounded_amount=Decimal("0.00100"),
    )
    extended = _ExtendedAdapter(
        "extended",
        min_order_size=Decimal("0.00010"),
        rounded_amount=Decimal("0.00100"),
    )
    opening_executor = _ScriptedExecutor()
    _, strategy, _, _ = _build_strategy(
        tmp_path,
        lighter=lighter,
        extended=extended,
        executor=opening_executor,
        position_tolerance=Decimal("0.00020"),
    )
    opened = asyncio.run(strategy.run_once(now=0.0))
    assert opened.action == "opened"

    closing_executor = _ScriptedExecutor(
        {
            "lighter": [{"actual_delta": "-0.00095"}],
            "extended": [{"actual_delta": "0.00095"}],
        }
    )
    _, restarted, _, _ = _build_strategy(
        tmp_path,
        lighter=lighter,
        extended=extended,
        executor=closing_executor,
        position_tolerance=Decimal("0.00020"),
    )

    with caplog.at_level(logging.WARNING):
        result = asyncio.run(restarted.run_once(now=7200.0))

    assert result.action == "closed"
    assert result.primary_size == Decimal("0.00005")
    assert result.hedge_size == Decimal("-0.00005")
    assert restarted.state.current_direction is None
    assert len(closing_executor.calls) == 2
    assert "低于最小下单量" in caplog.text


def test_tradeable_close_residual_is_not_hidden_by_configured_tolerance(
    tmp_path,
) -> None:
    """配置数值容差大于最小量时，仍必须平掉达到最小量的可交易残余。"""
    lighter = _LighterAdapter(
        "lighter",
        min_order_size=Decimal("0.00010"),
        rounded_amount=Decimal("0.00100"),
    )
    extended = _ExtendedAdapter(
        "extended",
        min_order_size=Decimal("0.00010"),
        rounded_amount=Decimal("0.00100"),
    )
    opening_executor = _ScriptedExecutor()
    _, strategy, _, _ = _build_strategy(
        tmp_path,
        lighter=lighter,
        extended=extended,
        executor=opening_executor,
        position_tolerance=Decimal("0.00020"),
    )
    opened = asyncio.run(strategy.run_once(now=0.0))
    assert opened.action == "opened"
    lighter.position = Decimal("0.00015")
    extended.position = Decimal("0")

    closing_executor = _ScriptedExecutor()
    _, restarted, _, _ = _build_strategy(
        tmp_path,
        lighter=lighter,
        extended=extended,
        executor=closing_executor,
        position_tolerance=Decimal("0.00020"),
    )

    result = asyncio.run(restarted.run_once(now=7200.0))

    assert result.action == "reconciled"
    assert lighter.position == 0
    assert extended.position == 0
    assert len(closing_executor.calls) == 1
    assert closing_executor.calls[0]["target_delta"] == Decimal("-0.00015")
    assert closing_executor.calls[0]["reduce_only"] is True


def test_minimum_query_failure_interlocks_without_uncertain_retry(
    tmp_path,
    caplog,
) -> None:
    """容差元数据未知时失败关闭并告警，不得落入成交未知死循环。"""
    lighter = _LighterAdapter(
        "lighter",
        min_order_size_error=RuntimeError("Lighter 市场元数据超时"),
    )
    extended = _ExtendedAdapter("extended")
    executor = _ScriptedExecutor()
    _, strategy, _, _ = _build_strategy(
        tmp_path,
        lighter=lighter,
        extended=extended,
        executor=executor,
    )

    with caplog.at_level(logging.WARNING):
        result = asyncio.run(strategy.run_once(now=0.0))

    assert result.action == "interlocked"
    assert result.action != "execution_uncertain"
    assert result.hedge_available is False
    assert executor.calls == []
    assert "对冲容差" in caplog.text
    assert "市场元数据超时" in caplog.text


def test_variational_missing_minimum_interlocks_without_submitting_orders(
    tmp_path,
    caplog,
) -> None:
    """Variational 未返回最小量时策略必须明确拒绝，不能按零下单。"""
    variational = object.__new__(VariationalClient)
    variational._quantity_limits = {}

    async def get_position(market: str):
        return Position(market, Decimal("0"))

    async def request_quote(
        underlying: str,
        side: str,
        qty: Decimal,
        *,
        instrument_type: str = "perpetual_future",
        funding_interval_s: int = 3600,
        kind: str | None = None,
    ):
        del underlying, side, qty, instrument_type, funding_interval_s, kind
        return {"margin_requirements": {"initial_margin": "0.2"}}

    async def get_supported_assets():
        return {"BTC": [{"asset": "BTC", "price": "60000"}]}

    variational.get_position = get_position
    variational.request_quote = request_quote
    variational.get_supported_assets = get_supported_assets
    executor = _ScriptedExecutor()
    _, strategy, _, _ = _build_strategy(
        tmp_path,
        lighter=variational,
        executor=executor,
    )

    with caplog.at_level(logging.WARNING):
        result = asyncio.run(strategy.run_once(now=0.0))

    assert result.action == "interlocked"
    assert result.hedge_available is False
    assert executor.calls == []
    # 只断言稳定的关键信息（字段名 + 拒绝语义），不锁死具体文案措辞。
    assert "min_qty" in caplog.text
    assert "拒绝" in caplog.text


def test_rfq_availability_does_not_require_orderbook_capabilities(tmp_path) -> None:
    """RFQ 对冲腿缺少限价、单查和撤单能力时仍可通过可用性检查。"""
    rfq = _RfqAdapter()
    assert not hasattr(rfq, "place_limit_order")
    assert not hasattr(rfq, "get_order_by_id")
    assert not hasattr(rfq, "cancel_order")
    executor = _ScriptedExecutor()
    _, strategy, lighter, _ = _build_strategy(
        tmp_path,
        extended=rfq,
        executor=executor,
    )

    result = asyncio.run(strategy.run_once(now=0.0))

    assert result.action == "opened"
    assert result.hedge_available is True
    assert lighter.position == -rfq.position != 0


@pytest.mark.parametrize(
    "capability",
    ["market_order", "get_market_price", "get_min_order_size"],
)
def test_rfq_availability_still_requires_common_capabilities(
    tmp_path,
    capability: str,
) -> None:
    """RFQ 也必须失败关闭缺失的公共交易能力。"""
    api = _api()
    rfq = _RfqAdapter()
    setattr(rfq, capability, None)
    _, strategy, _, _ = _build_strategy(tmp_path, extended=rfq)
    limits = api._OrderLimits(Decimal("0.001"), Decimal("0.001"))

    available = asyncio.run(strategy._check_hedge_available(limits))

    assert available is False
    assert strategy.hedge_interlock_active is True
    assert capability in strategy.hedge_interlock_reason


def test_mixed_execution_models_submit_both_legs_concurrently(tmp_path) -> None:
    """RFQ 与订单簿两腿用事件栅栏证明仍在同一节拍并发提交。"""

    async def scenario() -> tuple[object, object, _RfqAdapter, _LighterAdapter]:
        rfq_started = asyncio.Event()
        orderbook_started = asyncio.Event()

        class ConcurrentRfqAdapter(_RfqAdapter):
            async def market_order(
                self,
                market: str,
                side: Side,
                amount: Decimal,
                *,
                reduce_only: bool = False,
            ):
                rfq_started.set()
                await orderbook_started.wait()
                return await super().market_order(
                    market,
                    side,
                    amount,
                    reduce_only=reduce_only,
                )

        class ConcurrentOrderbookAdapter(_LighterAdapter):
            async def place_limit_order(
                self,
                market: str,
                side: Side,
                amount: Decimal,
                price: Decimal,
                *,
                post_only: bool = True,
                reduce_only: bool = False,
            ):
                orderbook_started.set()
                await rfq_started.wait()
                return await super().place_limit_order(
                    market,
                    side,
                    amount,
                    price,
                    post_only=post_only,
                    reduce_only=reduce_only,
                )

        rfq = ConcurrentRfqAdapter("variational")
        orderbook = ConcurrentOrderbookAdapter("lighter")
        _, strategy, _, _ = _build_strategy(
            tmp_path,
            lighter=rfq,
            extended=orderbook,
            maker_timeout_s=0.1,
        )
        results = await asyncio.wait_for(
            strategy._execute_pair(
                Decimal("1"),
                Decimal("-1"),
                reduce_only=False,
            ),
            timeout=0.5,
        )
        return results[0], results[1], rfq, orderbook

    rfq_result, orderbook_result, rfq, orderbook = asyncio.run(scenario())

    assert isinstance(rfq_result, HedgeFillResult)
    assert isinstance(orderbook_result, HedgeFillResult)
    assert rfq_result.used_taker is True
    assert orderbook_result.used_taker is False
    assert [event[0] for event in rfq.events] == ["market"]
    assert [event[0] for event in orderbook.events] == ["limit"]
    assert rfq.position == -orderbook.position == Decimal("1")


def test_restart_before_due_does_not_open_again(tmp_path) -> None:
    """1.8：未到期轮次重启后只等待原到期时刻。"""
    executor = _ScriptedExecutor()
    _, strategy, lighter, extended = _build_strategy(tmp_path, executor=executor)
    asyncio.run(strategy.run_once(now=0.0))

    restarted_executor = _ScriptedExecutor()
    _, restarted, _, _ = _build_strategy(
        tmp_path,
        lighter=lighter,
        extended=extended,
        executor=restarted_executor,
    )
    result = asyncio.run(restarted.run_once(now=7199.0))

    assert result.action == "wait"
    assert restarted_executor.calls == []


def test_restart_after_due_closes_immediately(tmp_path) -> None:
    """1.9：已过期轮次重启后第一轮立即平仓。"""
    executor = _ScriptedExecutor()
    _, strategy, lighter, extended = _build_strategy(tmp_path, executor=executor)
    asyncio.run(strategy.run_once(now=0.0))

    restarted_executor = _ScriptedExecutor()
    _, restarted, _, _ = _build_strategy(
        tmp_path,
        lighter=lighter,
        extended=extended,
        executor=restarted_executor,
    )
    result = asyncio.run(restarted.run_once(now=7201.0))

    assert result.action == "closed"
    assert lighter.position == 0
    assert extended.position == 0
    assert len(restarted_executor.calls) == 2


def test_persisted_state_mismatch_uses_actual_positions_and_warns(
    tmp_path,
    caplog,
) -> None:
    """1.10：记录称空仓但实仓已对冲时，以实仓恢复轮次并中文告警。"""
    api = _api()
    state_path = tmp_path / "state.json"
    state_path.write_text(
        json.dumps(
            {
                "round_index": 7,
                "last_direction": "short",
                "current_direction": None,
                "opened_at": None,
                "due_at": None,
            }
        ),
        encoding="utf-8",
    )
    lighter = _LighterAdapter("lighter")
    extended = _ExtendedAdapter("extended")
    lighter.position = Decimal("20")
    extended.position = Decimal("-20")
    executor = _ScriptedExecutor()
    config = api.TimedVolumeConfig(state_path=state_path, cycle_seconds=7200.0)
    strategy = api.TimedHedgedVolumeStrategy(
        lighter,
        extended,
        config,
        trade_executor=executor,
    )

    with caplog.at_level(logging.WARNING):
        result = asyncio.run(strategy.run_once(now=1000.0))

    assert result.action == "wait"
    assert strategy.state.current_direction is api.RoundDirection.LONG
    assert strategy.state.due_at == 8200.0
    assert executor.calls == []
    assert "持久化状态与实际持仓不一致" in caplog.text


def test_interlock_blocks_open_but_does_not_block_expired_close(tmp_path) -> None:
    """1.11：Extended 不可用只禁止开仓，不得阻断到期平仓。"""
    executor = _ScriptedExecutor()
    _, blocked, lighter, extended = _build_strategy(
        tmp_path,
        executor=executor,
        hedge_available=lambda: False,
    )
    skipped = asyncio.run(blocked.run_once(now=0.0))
    assert skipped.action == "interlocked"
    assert executor.calls == []

    healthy_executor = _ScriptedExecutor()
    _, healthy, _, _ = _build_strategy(
        tmp_path,
        lighter=lighter,
        extended=extended,
        executor=healthy_executor,
        hedge_available=lambda: True,
        state_name="open-state.json",
    )
    asyncio.run(healthy.run_once(now=0.0))

    close_executor = _ScriptedExecutor()
    _, expired, _, _ = _build_strategy(
        tmp_path,
        lighter=lighter,
        extended=extended,
        executor=close_executor,
        hedge_available=lambda: False,
        state_name="open-state.json",
    )
    closed = asyncio.run(expired.run_once(now=7200.0))
    assert closed.action == "closed"
    assert lighter.position == 0
    assert extended.position == 0


def test_orders_use_maker_first_and_timeout_then_market(tmp_path) -> None:
    """1.12：两腿先挂 maker，只有未成交并到达超时才补市价。"""
    lighter = _LighterAdapter("lighter", maker_fill=Decimal("0"))
    extended = _ExtendedAdapter("extended", maker_fill=Decimal("0"))
    _, strategy, _, _ = _build_strategy(
        tmp_path,
        lighter=lighter,
        extended=extended,
        maker_timeout_s=0.0,
    )

    result = asyncio.run(strategy.run_once(now=0.0))

    assert result.action == "opened"
    assert [event[0] for event in lighter.events] == ["limit", "cancel", "market"]
    assert [event[0] for event in extended.events] == ["limit", "cancel", "market"]
    assert lighter.events[0][5] is True
    assert extended.events[0][5] is True
    assert lighter.position == -extended.position != 0


def test_close_failure_rebuilds_other_leg_instead_of_leaving_naked(tmp_path) -> None:
    """平仓腿持续故障时，已平的一侧必须重建反向仓以恢复净敞口为零。"""
    opening_executor = _ScriptedExecutor()
    _, strategy, lighter, extended = _build_strategy(
        tmp_path,
        executor=opening_executor,
    )
    asyncio.run(strategy.run_once(now=0.0))

    closing_executor = _ScriptedExecutor(
        {
            "extended": [
                {"fraction": "0", "error": "Extended 平仓失败"},
                {"fraction": "0", "error": "Extended 平仓失败"},
                {"fraction": "0", "error": "Extended 平仓失败"},
                {"fraction": "0", "error": "Extended 平仓失败"},
            ]
        }
    )
    _, restarted, _, _ = _build_strategy(
        tmp_path,
        lighter=lighter,
        extended=extended,
        executor=closing_executor,
    )

    result = asyncio.run(restarted.run_once(now=7200.0))

    lighter_calls = [
        call for call in closing_executor.calls if call["adapter"] == "lighter"
    ]
    assert result.action == "close_failed_neutral"
    assert lighter_calls[-1]["target_delta"] == Decimal("20.000")
    assert lighter.position == Decimal("20.000")
    assert extended.position == Decimal("-20.000")
    assert result.net_exposure == 0


def test_one_day_replay_has_twelve_alternating_rounds_and_48000_volume(
    tmp_path,
) -> None:
    """4.3：2 小时、2000 美元回放一天，完成 12 轮且刷量 48000 美元。"""
    executor = _ScriptedExecutor()
    api, strategy, _, _ = _build_strategy(tmp_path, executor=executor)
    directions = []
    volume = Decimal("0")

    opened = asyncio.run(strategy.run_once(now=0.0))
    directions.append(opened.direction)
    volume += Decimal("2000")
    for round_number in range(1, 13):
        boundary = round_number * 7200.0
        closed = asyncio.run(strategy.run_once(now=boundary))
        assert closed.action == "closed"
        volume += Decimal("2000")
        if round_number < 12:
            opened = asyncio.run(strategy.run_once(now=boundary))
            assert opened.action == "opened"
            directions.append(opened.direction)
            volume += Decimal("2000")

    assert len(directions) == 12
    assert directions == [
        api.RoundDirection.LONG if index % 2 == 0 else api.RoundDirection.SHORT
        for index in range(12)
    ]
    assert volume == Decimal("48000")


def test_restart_partial_position_repair_keeps_original_due_time(tmp_path) -> None:
    """未到期轮次按实仓差修复后不得重置计时、延长持仓周期。"""
    opening_executor = _ScriptedExecutor()
    _, strategy, lighter, extended = _build_strategy(
        tmp_path,
        executor=opening_executor,
    )
    asyncio.run(strategy.run_once(now=0.0))
    lighter.position = Decimal("8")

    repair_executor = _ScriptedExecutor()
    _, restarted, _, _ = _build_strategy(
        tmp_path,
        lighter=lighter,
        extended=extended,
        executor=repair_executor,
    )
    result = asyncio.run(restarted.run_once(now=1000.0))

    assert result.action == "reconciled"
    assert repair_executor.calls[0]["target_delta"] == Decimal("12.000")
    assert restarted.state.due_at == 7200.0
    assert result.net_exposure == 0


def test_post_order_position_read_failure_is_retried_and_converged(tmp_path) -> None:
    """下单后暂时读不到实仓时进程不得退出，下一节拍须按恢复的实仓回滚。"""

    class FlakyLighter(_LighterAdapter):
        def __init__(self):
            super().__init__("lighter")
            self.read_count = 0

        async def get_position(self, market: str):
            self.read_count += 1
            if self.read_count == 3:
                raise RuntimeError("下单后持仓暂不可读")
            return await super().get_position(market)

    lighter = FlakyLighter()
    extended = _ExtendedAdapter("extended")
    executor = _ScriptedExecutor(
        {
            "lighter": [{}, {}],
            "extended": [{"fraction": "0", "error": "对冲失败"}],
        }
    )
    _, strategy, _, _ = _build_strategy(
        tmp_path,
        lighter=lighter,
        extended=extended,
        executor=executor,
    )

    uncertain = asyncio.run(strategy.run_once(now=0.0))
    assert uncertain.action == "execution_uncertain"
    assert uncertain.net_exposure is None

    converged = asyncio.run(strategy.run_once(now=1.0))
    assert converged.action == "reconciled"
    assert lighter.position == 0
    assert extended.position == 0
    assert converged.net_exposure == 0


def test_default_interlock_blocks_open_when_extended_market_is_unavailable(
    tmp_path,
) -> None:
    """Extended 仅持仓可读但交易行情不可用时仍应失败关闭。"""

    class UnavailableExtended(_ExtendedAdapter):
        async def get_market_price(self, market_name: str):
            raise RuntimeError("Extended 行情不可用")

    executor = _ScriptedExecutor()
    _, strategy, _, _ = _build_strategy(
        tmp_path,
        extended=UnavailableExtended("extended"),
        executor=executor,
    )

    result = asyncio.run(strategy.run_once(now=0.0))

    assert result.action == "interlocked"
    assert result.hedge_available is False
    assert "行情不可用" in result.interlock_reason
    assert executor.calls == []
