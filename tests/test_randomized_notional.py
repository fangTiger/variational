"""每轮随机名义额测试；全程复用已核对真实签名的内存适配器。"""

from __future__ import annotations

import asyncio
import json
import random
from decimal import Decimal

import pytest

from tests.test_timed_hedged_volume import (
    _ExtendedAdapter,
    _LighterAdapter,
    _ScriptedExecutor,
)
from timed_volume.strategy import TimedHedgedVolumeStrategy, TimedVolumeConfig


class _SequenceRandom:
    """依次返回预设整数，并校验策略传入的闭区间。"""

    def __init__(self, values: list[int]) -> None:
        self.values = list(values)
        self.calls: list[tuple[int, int]] = []

    def __call__(self, lower: int, upper: int) -> int:
        self.calls.append((lower, upper))
        value = self.values.pop(0)
        assert lower <= value <= upper
        return value


def _build_strategy(
    tmp_path,
    *,
    notional_min_usd: int = 2000,
    notional_max_usd: int = 2300,
    random_int=None,
    lighter: _LighterAdapter | None = None,
    extended: _ExtendedAdapter | None = None,
    executor: _ScriptedExecutor | None = None,
):
    """以真实策略构造签名装配随机名义额测试对象。"""
    lighter = lighter or _LighterAdapter("lighter")
    extended = extended or _ExtendedAdapter("extended")
    executor = executor or _ScriptedExecutor()
    config = TimedVolumeConfig(
        notional_min_usd=notional_min_usd,
        notional_max_usd=notional_max_usd,
        cycle_seconds=7200.0,
        maker_poll_s=0.0,
        state_path=tmp_path / "state.json",
    )
    strategy = TimedHedgedVolumeStrategy(
        lighter,
        extended,
        config,
        trade_executor=executor,
        random_int=random_int,
    )
    return strategy, lighter, extended, executor


def test_notional_lower_bound_above_upper_bound_is_rejected() -> None:
    """1.1：下限大于上限时在配置构造阶段给出中文错误。"""
    with pytest.raises(ValueError, match="下限.*不得大于.*上限"):
        TimedVolumeConfig(notional_min_usd=2301, notional_max_usd=2300)


def test_non_positive_notional_lower_bound_is_rejected() -> None:
    """1.2：名义额下限为非正数时拒绝构造。"""
    with pytest.raises(ValueError, match="名义额.*大于零"):
        TimedVolumeConfig(notional_min_usd=0, notional_max_usd=2300)


def test_non_positive_notional_upper_bound_is_rejected() -> None:
    """1.2：名义额上限为非正数时拒绝构造。"""
    with pytest.raises(ValueError, match="名义额.*大于零"):
        TimedVolumeConfig(notional_min_usd=-2, notional_max_usd=-1)


def test_opened_round_notional_is_an_integer_inside_closed_interval(tmp_path) -> None:
    """1.3：默认随机源产生的每轮金额是闭区间内的整数美元。"""
    strategy, _, _, _ = _build_strategy(
        tmp_path,
        random_int=random.Random(7).randint,
    )

    result = asyncio.run(strategy.run_once(now=0.0))

    assert result.action == "opened"
    assert isinstance(strategy.state.current_notional_usd, int)
    assert 2000 <= strategy.state.current_notional_usd <= 2300


def test_single_point_notional_interval_matches_fixed_notional(tmp_path) -> None:
    """1.4：区间退化为单点时仍按原固定金额建立中性双腿。"""
    strategy, lighter, extended, executor = _build_strategy(
        tmp_path,
        notional_min_usd=2000,
        notional_max_usd=2000,
        random_int=_SequenceRandom([2000]),
    )

    result = asyncio.run(strategy.run_once(now=0.0))

    assert result.action == "opened"
    assert strategy.state.current_notional_usd == 2000
    assert executor.calls[0]["target_delta"] == Decimal("20.000")
    assert executor.calls[1]["target_delta"] == Decimal("-20.000")
    assert lighter.position == -extended.position


def test_each_new_round_samples_notional_independently(tmp_path) -> None:
    """1.5：完成一轮后，新轮次必须再次调用随机源。"""
    random_int = _SequenceRandom([2001, 2299])
    strategy, _, _, _ = _build_strategy(tmp_path, random_int=random_int)

    first = asyncio.run(strategy.run_once(now=0.0))
    first_notional = strategy.state.current_notional_usd
    closed = asyncio.run(strategy.run_once(now=7200.0))
    second = asyncio.run(strategy.run_once(now=7200.0))

    assert first.action == "opened"
    assert closed.action == "closed"
    assert second.action == "opened"
    assert first_notional == 2001
    assert strategy.state.current_notional_usd == 2299
    assert random_int.calls == [(2000, 2300), (2000, 2300)]


def test_both_legs_use_same_sampled_notional_and_remain_neutral(tmp_path) -> None:
    """1.6：同一轮两腿只共享一次取值，并保持净敞口为零。"""
    random_int = _SequenceRandom([2173])
    strategy, _, _, executor = _build_strategy(tmp_path, random_int=random_int)

    result = asyncio.run(strategy.run_once(now=0.0))

    open_calls = [call for call in executor.calls if not call["reduce_only"]]
    assert random_int.calls == [(2000, 2300)]
    assert len(open_calls) == 2
    assert abs(open_calls[0]["target_delta"]) == abs(open_calls[1]["target_delta"])
    assert abs(open_calls[0]["target_delta"]) * Decimal("100") == Decimal("2173.000")
    assert result.net_exposure == 0


def test_restart_before_due_reuses_persisted_notional_without_sampling(
    tmp_path,
) -> None:
    """1.7：未到期轮次重启后沿用状态金额，不再次调用随机源。"""
    strategy, lighter, extended, _ = _build_strategy(
        tmp_path,
        random_int=_SequenceRandom([2188]),
    )
    asyncio.run(strategy.run_once(now=0.0))
    persisted = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))

    def unexpected_random(_lower: int, _upper: int) -> int:
        raise AssertionError("恢复未到期轮次时不得重新随机")

    restarted, _, _, executor = _build_strategy(
        tmp_path,
        lighter=lighter,
        extended=extended,
        random_int=unexpected_random,
    )
    result = asyncio.run(restarted.run_once(now=7199.0))

    assert persisted["current_notional_usd"] == 2188
    assert restarted.state.current_notional_usd == 2188
    assert result.action == "wait"
    assert executor.calls == []


def test_notional_is_reused_after_exit_before_pair_submission(tmp_path) -> None:
    """金额落盘后、双腿提交前退出，重启仍须复用原取值。"""

    class SimulatedProcessExit(BaseException):
        """模拟无法被策略异常处理捕获的进程中断。"""

    strategy, lighter, extended, _ = _build_strategy(
        tmp_path,
        random_int=_SequenceRandom([2211]),
    )

    async def exit_before_pair(
        _primary_delta: Decimal,
        _hedge_delta: Decimal,
        *,
        reduce_only: bool,
    ) -> tuple[object, object]:
        assert reduce_only is False
        persisted = json.loads(
            (tmp_path / "state.json").read_text(encoding="utf-8")
        )
        assert persisted["current_notional_usd"] == 2211
        raise SimulatedProcessExit

    strategy._execute_pair = exit_before_pair
    with pytest.raises(SimulatedProcessExit):
        asyncio.run(strategy.run_once(now=0.0))

    def unexpected_random(_lower: int, _upper: int) -> int:
        raise AssertionError("重启恢复待开轮次时不得重新随机")

    restarted, _, _, executor = _build_strategy(
        tmp_path,
        lighter=lighter,
        extended=extended,
        random_int=unexpected_random,
    )
    result = asyncio.run(restarted.run_once(now=1.0))

    assert result.action == "opened"
    assert restarted.state.current_notional_usd == 2211
    assert [call["target_delta"] for call in executor.calls] == [
        Decimal("22.110"),
        Decimal("-22.110"),
    ]


def test_close_uses_actual_positions_instead_of_persisted_notional(tmp_path) -> None:
    """1.8：到期平仓量来自两侧实仓，与轮次记录金额无关。"""
    strategy, lighter, extended, _ = _build_strategy(
        tmp_path,
        random_int=_SequenceRandom([2200]),
    )
    asyncio.run(strategy.run_once(now=0.0))
    lighter.position = Decimal("7.125")
    extended.position = Decimal("-7.125")

    close_executor = _ScriptedExecutor()
    restarted, _, _, _ = _build_strategy(
        tmp_path,
        lighter=lighter,
        extended=extended,
        executor=close_executor,
        random_int=_SequenceRandom([2300]),
    )
    result = asyncio.run(restarted.run_once(now=7200.0))

    assert result.action == "closed"
    assert [call["target_delta"] for call in close_executor.calls] == [
        Decimal("-7.125"),
        Decimal("7.125"),
    ]
    assert all(call["reduce_only"] for call in close_executor.calls)


def test_injected_random_source_makes_sampled_notional_deterministic(tmp_path) -> None:
    """1.9：注入固定随机源时可确定性得到预期金额。"""
    random_int = _SequenceRandom([2246])
    strategy, _, _, _ = _build_strategy(tmp_path, random_int=random_int)

    result = asyncio.run(strategy.run_once(now=0.0))

    assert result.action == "opened"
    assert strategy.state.current_notional_usd == 2246
    assert random_int.calls == [(2000, 2300)]
