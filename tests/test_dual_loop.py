"""Task 7：2.5 秒快循环与 30 秒慢循环回归测试。"""
from __future__ import annotations

import asyncio
import logging
from decimal import Decimal
from types import SimpleNamespace

from adapters.base import Side
from grid.grid_engine import GridConfig, GridEngine
from grid.grid_state import GridState
from grid.regime import GridMode
from tools.run_grid import _build_parser, _grid_config


def _active_engine(tmp_path) -> GridEngine:
    eng = GridEngine(
        SimpleNamespace(),
        GridConfig(
            dry_run=False,
            trend_aware=True,
            state_path=str(tmp_path / "grid_state.json"),
        ),
    )
    eng._state = GridState(95.0, 105.0, False, None, False)
    eng._mode = GridMode.NEUTRAL
    return eng


def test_fast_cycle_skips_candles_but_runs_tpsl_and_fills(
    tmp_path,
    monkeypatch,
) -> None:
    """快循环只做执行链，不得读取 K 线或补格。"""
    calls = []
    eng = _active_engine(tmp_path)

    async def fake_inv():
        calls.append("position_mark")
        return Decimal("0.01"), 100.0

    async def fake_hard_stop(*, signed_size, mark):
        calls.append("hard_stop")
        return False

    async def fake_tpsl(mark, signed_size):
        calls.append("tpsl")
        return True

    async def fake_fills(inv_usd, blocked_side=None):
        calls.append("fills")

    async def fail_slow(*args, **kwargs):
        raise AssertionError("快循环不得进入 K 线后的慢路径")

    class Info:
        async def get_candles_history(self, **kwargs):
            raise AssertionError("快循环不得拉 K 线")

    eng.ext._client = SimpleNamespace(info=Info())
    monkeypatch.setattr(eng, "_inv", fake_inv)
    monkeypatch.setattr(eng, "_check_hard_stop", fake_hard_stop)
    monkeypatch.setattr(eng, "_maintain_tpsl", fake_tpsl)
    monkeypatch.setattr(eng, "_handle_fills", fake_fills)
    monkeypatch.setattr(eng, "_advance_band", fail_slow)
    monkeypatch.setattr(eng, "_maintain_ladder", fail_slow)

    result = asyncio.run(eng.run_once(include_slow=False))

    assert calls == ["position_mark", "hard_stop", "tpsl", "fills"]
    assert result == "快速执行完成"


def test_slow_cycle_runs_twice_in_twenty_four_fast_rounds(
    tmp_path,
    monkeypatch,
) -> None:
    """24 个 2.5 秒轮次中，30 秒慢路径应触发 2 次。"""
    eng = _active_engine(tmp_path)
    clock = {"now": 100.0}
    candle_calls = 0
    rounds = 0

    candles = [
        SimpleNamespace(timestamp=str(i), high=101.0, low=99.0, close=100.0)
        for i in range(20)
    ]

    class Info:
        async def get_candles_history(self, **kwargs):
            nonlocal candle_calls
            candle_calls += 1
            return SimpleNamespace(data=candles)

    eng.ext._client = SimpleNamespace(info=Info())
    original_run_once = eng.run_once

    async def fake_connect():
        return None

    async def scheduled_run_once(*, include_slow=True):
        nonlocal rounds
        rounds += 1
        result = await original_run_once(include_slow=include_slow)
        if rounds == 24:
            eng.stop()
        return result

    async def fake_inv():
        return Decimal("0"), 100.0

    async def no_hard_stop(*, signed_size, mark):
        return False

    async def confirmed_tpsl(mark, signed_size):
        return True

    async def no_op(*args, **kwargs):
        return None

    async def ladder_ready(*args, **kwargs):
        return "阶梯已就位"

    async def fake_sleep(seconds):
        clock["now"] += seconds

    monkeypatch.setattr(eng, "connect", fake_connect)
    monkeypatch.setattr(eng, "run_once", scheduled_run_once)
    monkeypatch.setattr(eng, "_inv", fake_inv)
    monkeypatch.setattr(eng, "_check_hard_stop", no_hard_stop)
    monkeypatch.setattr(eng, "_maintain_tpsl", confirmed_tpsl)
    monkeypatch.setattr(eng, "_handle_fills", no_op)
    monkeypatch.setattr(eng, "_advance_band", no_op)
    monkeypatch.setattr(eng, "_maintain_ladder", ladder_ready)
    monkeypatch.setattr(eng, "_dump_live", no_op)
    monkeypatch.setattr(
        "grid.grid_engine.trend_gate",
        lambda *args, **kwargs: GridMode.NEUTRAL,
    )
    monkeypatch.setattr("grid.grid_engine.time.monotonic", lambda: clock["now"])
    monkeypatch.setattr("grid.grid_engine.asyncio.sleep", fake_sleep)

    asyncio.run(eng.run_forever())

    assert rounds == 24
    assert candle_calls == 2


def test_absolute_deadlines_prevent_runtime_drift(tmp_path, monkeypatch) -> None:
    """单轮略超周期后，后续轮次仍回到绝对时间节拍。"""
    eng = _active_engine(tmp_path)
    clock = {"now": 100.0}
    starts = []
    runtimes = iter((3.0, 1.0, 0.0))

    async def fake_connect():
        return None

    async def fake_run_once(*, include_slow=True):
        starts.append(clock["now"])
        clock["now"] += next(runtimes)
        if len(starts) == 3:
            eng.stop()
        return "ok"

    async def fake_sleep(seconds):
        clock["now"] += seconds

    monkeypatch.setattr(eng, "connect", fake_connect)
    monkeypatch.setattr(eng, "run_once", fake_run_once)
    monkeypatch.setattr("grid.grid_engine.time.monotonic", lambda: clock["now"])
    monkeypatch.setattr("grid.grid_engine.asyncio.sleep", fake_sleep)

    asyncio.run(eng.run_forever())

    assert starts == [100.0, 103.0, 105.0]


def test_large_overrun_drops_missed_rounds_without_busy_catchup(
    tmp_path,
    monkeypatch,
) -> None:
    """落后超过一周期时只立即跑一次，不能连续补跑已丢轮次。"""
    eng = _active_engine(tmp_path)
    clock = {"now": 100.0}
    starts = []
    runtimes = iter((10.0, 0.0, 0.0))

    async def fake_connect():
        return None

    async def fake_run_once(*, include_slow=True):
        starts.append(clock["now"])
        clock["now"] += next(runtimes)
        if len(starts) == 3:
            eng.stop()
        return "ok"

    async def fake_sleep(seconds):
        clock["now"] += seconds

    monkeypatch.setattr(eng, "connect", fake_connect)
    monkeypatch.setattr(eng, "run_once", fake_run_once)
    monkeypatch.setattr("grid.grid_engine.time.monotonic", lambda: clock["now"])
    monkeypatch.setattr("grid.grid_engine.asyncio.sleep", fake_sleep)

    asyncio.run(eng.run_forever())

    assert starts == [100.0, 110.0, 112.5]


def test_hard_stop_still_short_circuits_fast_cycle(tmp_path, monkeypatch) -> None:
    """快循环中的硬止损触发后，不得继续 TPSL、成交处理或慢路径。"""
    eng = _active_engine(tmp_path)
    calls = []

    async def fake_inv():
        return Decimal("0.01"), 100.0

    async def trigger_hard_stop(*, signed_size, mark):
        calls.append("hard_stop")
        return True

    async def forbidden(*args, **kwargs):
        raise AssertionError("硬止损后不得继续执行")

    monkeypatch.setattr(eng, "_inv", fake_inv)
    monkeypatch.setattr(eng, "_check_hard_stop", trigger_hard_stop)
    monkeypatch.setattr(eng, "_maintain_tpsl", forbidden)
    monkeypatch.setattr(eng, "_handle_fills", forbidden)

    result = asyncio.run(eng.run_once(include_slow=False))

    assert calls == ["hard_stop"]
    assert result == "HALTED：硬止损已触发"


def test_inventory_cap_still_blocks_fast_cycle_retry(tmp_path, monkeypatch) -> None:
    """快循环成交重试仍须经过库存上限，不能因提速绕过风控。"""

    class Ext:
        def __init__(self) -> None:
            self.placed = []

        async def get_open_orders(self, market):
            return []

        async def get_orders_history(self, market, limit=100, **kwargs):
            return []

        async def place_limit_order(self, *args, **kwargs):
            self.placed.append((args, kwargs))
            return SimpleNamespace(data=SimpleNamespace(id="unexpected", status="NEW"))

    ext = Ext()
    eng = GridEngine(
        ext,
        GridConfig(
            dry_run=False,
            trend_aware=True,
            unit_usd=50.0,
            max_inventory_usd=100.0,
            state_path=str(tmp_path / "grid_state.json"),
        ),
    )
    eng._state = GridState(95.0, 105.0, False, None, False)
    eng._mode = GridMode.NEUTRAL
    eng._retry[1] = {
        "side": Side.BUY,
        "qty": Decimal("0.5"),
        "why": "成交翻单",
        "attempts": 0,
        "requested_at": 0.0,
    }

    async def fake_inv():
        return Decimal("1"), 100.0

    async def no_hard_stop(*, signed_size, mark):
        return False

    async def confirmed_tpsl(mark, signed_size):
        return True

    monkeypatch.setattr(eng, "_inv", fake_inv)
    monkeypatch.setattr(eng, "_check_hard_stop", no_hard_stop)
    monkeypatch.setattr(eng, "_maintain_tpsl", confirmed_tpsl)

    asyncio.run(eng.run_once(include_slow=False))

    assert ext.placed == []
    assert 1 in eng._retry
    assert eng._within_cap(Side.BUY, inv_usd=100.0) is False


def test_polling_and_slow_interval_defaults() -> None:
    """配置与 CLI 默认值保持 2.5 秒快循环、30 秒慢循环。"""
    defaults = GridConfig()
    args = _build_parser().parse_args([])

    assert defaults.poll_interval == 2.5
    assert defaults.slow_interval == 30.0
    assert args.interval == 2.5
    assert args.slow_interval == 30.0
    assert _grid_config(args).slow_interval == 30.0


def test_connectivity_becomes_critical_only_after_two_slow_intervals(
    tmp_path,
    monkeypatch,
    caplog,
) -> None:
    """连续失败字段逐轮累加，但只有失联满 60 秒才升级告警。"""
    eng = GridEngine(
        object(),
        GridConfig(
            state_path=str(tmp_path / "grid_state.json"),
            poll_interval=2.5,
            slow_interval=30.0,
        ),
    )
    clock = {"now": 1_000.0}
    attempts = 0
    eng._last_success_ts = clock["now"]

    async def fake_connect():
        return None

    async def fail_for_sixty_seconds():
        nonlocal attempts
        attempts += 1
        if attempts == 25:
            eng.stop()
        raise ConnectionError("connection reset")

    async def fake_sleep(seconds):
        clock["now"] += seconds

    monkeypatch.setattr(eng, "connect", fake_connect)
    monkeypatch.setattr(eng, "run_once", fail_for_sixty_seconds)
    monkeypatch.setattr("grid.grid_engine.time.monotonic", lambda: clock["now"])
    monkeypatch.setattr("grid.grid_engine.time.time", lambda: clock["now"])
    monkeypatch.setattr("grid.grid_engine.asyncio.sleep", fake_sleep)

    with caplog.at_level(logging.CRITICAL, logger="grid_engine"):
        asyncio.run(eng.run_forever())

    critical = [
        record for record in caplog.records if record.levelno == logging.CRITICAL
    ]
    assert attempts == 25
    assert eng._consecutive_failures == 25
    assert len(critical) == 1
    assert "已失联 1.0 分钟" in critical[0].getMessage()
