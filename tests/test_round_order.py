"""Task 4：请求超时、执行优先顺序与失联状态回归测试。"""
from __future__ import annotations

import asyncio
import json
import logging
from decimal import Decimal
from types import SimpleNamespace

import pytest

from adapters import extended_client
from adapters.base import Position
from grid.grid_engine import GridConfig, GridEngine
from grid.grid_state import GridState
from grid.regime import GridMode
from tools.run_grid import _build_parser


def test_request_timeout_is_bounded() -> None:
    """SDK 默认 500 秒会让一次卡顿冻结整个风控循环，必须收紧。

    上界取 30 秒：2026-08-10 实测连接重建时 TLS 握手 0.5~3.1s、整请求可达
    7.4s，原来的 10 秒只有约 1.5 倍余量，抖动即超时导致 TPSL 维护失败，
    故放宽到 25 秒；但仍需远小于 SDK 默认值，避免卡死风控循环。
    """
    timeout = (
        extended_client.build_client_config("mainnet")
        .defaults.request_timeout_seconds
    )
    assert 10 <= timeout <= 30


def test_candle_failure_still_maintains_tpsl_and_handles_fills(
    tmp_path,
    monkeypatch,
) -> None:
    """K 线故障只能阻止更新 band 和补格，不能阻止保护性执行。"""
    calls = []

    class Info:
        async def get_candles_history(self, **kwargs):
            calls.append("candles")
            raise RuntimeError("candle 504")

    eng = GridEngine(
        SimpleNamespace(_client=SimpleNamespace(info=Info())),
        GridConfig(
            dry_run=False,
            trend_aware=True,
            state_path=str(tmp_path / "grid_state.json"),
        ),
    )
    eng._state = GridState(95.0, 105.0, False, None, False)
    eng._mode = GridMode.OFF

    async def fake_inv():
        calls.append("inv")
        return Decimal("0.01"), 100.0

    async def fake_hard_stop(*, signed_size, mark):
        calls.append(("hard_stop", signed_size, mark))
        return False

    async def fake_tpsl(mark, signed_size):
        calls.append(("tpsl", mark, signed_size))
        return True

    async def fake_fills(inv_usd, blocked_side=None):
        calls.append(("fills", inv_usd, blocked_side))

    async def fail_ladder(*args, **kwargs):
        raise AssertionError("K 线失败后不得补格")

    monkeypatch.setattr(eng, "_inv", fake_inv)
    monkeypatch.setattr(eng, "_check_hard_stop", fake_hard_stop)
    monkeypatch.setattr(eng, "_maintain_tpsl", fake_tpsl)
    monkeypatch.setattr(eng, "_handle_fills", fake_fills)
    monkeypatch.setattr(eng, "_maintain_ladder", fail_ladder)

    result = asyncio.run(eng.run_once())

    assert calls == [
        "inv",
        ("hard_stop", Decimal("0.01"), 100.0),
        ("tpsl", 100.0, Decimal("0.01")),
        ("fills", 1.0, "BOTH"),
        "candles",
    ]
    assert result == "K线获取失败：已处理成交，跳过补格"


def test_inv_prefers_realtime_mark_and_keeps_position_size(tmp_path) -> None:
    """实时 mark 优先，仓位数量与清算价仍复用 position。"""

    class Ext:
        def __init__(self) -> None:
            self.position_calls = 0
            self.liquidation_calls = 0
            self.market_statistics_calls = 0

        async def get_mark_price(self, market):
            """引擎已改为走适配器接口取标记价，不再直接摸 SDK。"""
            self.market_statistics_calls += 1
            return Decimal("101")

        async def get_position(self, market):
            self.position_calls += 1
            return Position(
                market=market,
                signed_size=Decimal("0.0123"),
                raw=SimpleNamespace(mark_price="95", liquidation_price="80"),
            )

        async def get_liquidation_info(self, market):
            self.liquidation_calls += 1
            return Decimal("100"), Decimal("80")

    ext = Ext()
    eng = GridEngine(
        ext,
        GridConfig(
            dry_run=True,
            trend_aware=True,
            recenter_bars=1,
            state_path=str(tmp_path / "grid_state.json"),
        ),
    )
    eng._state = GridState(95.0, 105.0, True, "BUY", False)

    inv, mark = asyncio.run(eng._inv())

    assert inv == Decimal("0.0123")
    assert mark == 101.0
    assert ext.position_calls == 1
    assert ext.market_statistics_calls == 1
    assert ext.liquidation_calls == 0


@pytest.mark.parametrize("realtime_result", [RuntimeError("行情超时"), "0"])
def test_inv_falls_back_to_position_mark_and_warns(
    tmp_path,
    caplog,
    realtime_result,
) -> None:
    """实时行情异常或返回零时，回退 position mark 并明确告警。"""

    async def get_mark_price(market):
        if isinstance(realtime_result, Exception):
            raise realtime_result
        return Decimal(str(realtime_result))

    position = Position(
        market="BTC-USD",
        signed_size=Decimal("0.0123"),
        raw=SimpleNamespace(mark_price="95", liquidation_price="80"),
    )
    ext = SimpleNamespace(get_mark_price=get_mark_price)
    eng = GridEngine(ext, GridConfig(state_path=str(tmp_path / "grid_state.json")))

    with caplog.at_level(logging.WARNING, logger="grid_engine"):
        inv, mark = asyncio.run(eng._inv(position=position))

    assert inv == Decimal("0.0123")
    assert mark == 95.0
    assert any(
        record.levelno == logging.WARNING
        and "实时 mark 获取失败" in record.message
        and "回落到 position.mark_price" in record.message
        for record in caplog.records
    )


def test_hard_stop_prefers_realtime_mark_over_position_mark(tmp_path) -> None:
    """硬止损直接接收 position 时，也不得使用其中的陈旧 mark。"""

    async def get_mark_price(market):
        return Decimal("100")

    position = Position(
        market="BTC-USD",
        signed_size=Decimal("0.01"),
        raw=SimpleNamespace(mark_price="90", liquidation_price="80"),
    )
    ext = SimpleNamespace(get_mark_price=get_mark_price)
    eng = GridEngine(
        ext,
        GridConfig(
            dry_run=True,
            hard_stop_dist=0.05,
            state_path=str(tmp_path / "grid_state.json"),
        ),
    )

    triggered = asyncio.run(eng._check_hard_stop(position=position))

    assert triggered is False
    assert eng._last_mark == 100.0
    assert eng._last_dist_to_liq == pytest.approx(0.20)


def test_failed_rounds_increment_counter_and_write_live_snapshot(
    tmp_path,
    monkeypatch,
    caplog,
) -> None:
    """短时连续失败写入快照，但未满 60 秒不得升级 CRITICAL。"""
    eng = GridEngine(
        object(),
        GridConfig(state_path=str(tmp_path / "grid_state.json"), poll_interval=0),
    )
    attempts = 0

    async def fake_connect():
        return None

    async def fail_five_times():
        nonlocal attempts
        attempts += 1
        if attempts == 5:
            eng.stop()
        raise ConnectionError("connection reset")

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(eng, "connect", fake_connect)
    monkeypatch.setattr(eng, "run_once", fail_five_times)
    monkeypatch.setattr("grid.grid_engine.asyncio.sleep", no_sleep)

    with caplog.at_level(logging.CRITICAL, logger="grid_engine"):
        asyncio.run(eng.run_forever())

    live = json.loads((tmp_path / "grid_live.json").read_text(encoding="utf-8"))
    assert eng._consecutive_failures == 5
    assert live["consecutive_failures"] == 5
    assert live["last_success_ts"] == eng._last_success_ts
    assert "已失联" not in caplog.text


def test_successful_round_resets_failure_counter_and_updates_last_success(
    tmp_path,
    monkeypatch,
) -> None:
    """恢复成功后的第一轮立即清零失联状态并刷新成功时间。"""
    eng = GridEngine(
        object(),
        GridConfig(state_path=str(tmp_path / "grid_state.json"), poll_interval=0),
    )
    eng._last_success_ts = 0.0
    attempts = 0

    async def fake_connect():
        return None

    async def fail_then_succeed():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ConnectionError("connection reset")
        eng.stop()
        return "ok"

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(eng, "connect", fake_connect)
    monkeypatch.setattr(eng, "run_once", fail_then_succeed)
    monkeypatch.setattr("grid.grid_engine.asyncio.sleep", no_sleep)

    asyncio.run(eng.run_forever())

    live = json.loads((tmp_path / "grid_live.json").read_text(encoding="utf-8"))
    assert eng._consecutive_failures == 0
    assert eng._last_success_ts > 0
    assert live["consecutive_failures"] == 0
    assert live["last_success_ts"] == eng._last_success_ts


def test_polling_defaults_match_dual_loop_intervals() -> None:
    """引擎配置和命令行默认快慢轮询间隔保持一致。"""
    assert GridConfig().poll_interval == 2.5
    assert GridConfig().slow_interval == 30.0
    assert _build_parser().parse_args([]).interval == 2.5
    assert _build_parser().parse_args([]).slow_interval == 30.0
