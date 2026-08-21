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

from adapters.base import MarketPrice, Position, Side
from adapters.extended_client import ExtendedClient
from adapters.lighter_client import LighterClient
from engine.hedge_engine import HedgeFillResult


class _AdapterCore:
    """共享的内存持仓与订单行为；公开方法由两种真实签名的子类提供。"""

    def __init__(self, name: str, *, maker_fill: Decimal = Decimal("1")) -> None:
        self.name = name
        self.position = Decimal("0")
        self.maker_fill = Decimal(str(maker_fill))
        self.events: list[tuple] = []
        self._orders: dict[str, SimpleNamespace] = {}

    async def get_market_price(self, market: str):
        return MarketPrice(market, bid=Decimal("99"), ask=Decimal("101"))

    async def get_position(self, market: str):
        return Position(market, self.position)

    async def get_min_order_size(self, market: str):
        del market
        return Decimal("0.001")

    async def round_amount(self, market: str, amount: Decimal):
        del market
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
        actual_delta = target_delta * fraction
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
):
    api = _api()
    lighter = lighter or _LighterAdapter("lighter")
    extended = extended or _ExtendedAdapter("extended")
    config = api.TimedVolumeConfig(
        primary_market="BTC",
        hedge_market="BTC-USD",
        notional_usd=Decimal("2000"),
        cycle_seconds=7200.0,
        initial_direction=api.RoundDirection(initial_direction),
        maker_timeout_s=maker_timeout_s,
        maker_poll_s=0.0,
        position_tolerance=Decimal("0.000001"),
        state_path=tmp_path / state_name,
    )
    strategy = api.TimedHedgedVolumeStrategy(
        lighter,
        extended,
        config,
        trade_executor=executor,
        hedge_available=hedge_available,
    )
    return api, strategy, lighter, extended


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
