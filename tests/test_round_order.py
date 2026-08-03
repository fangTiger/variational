"""Task 4：请求超时、执行优先顺序与失联状态回归测试。"""
from __future__ import annotations

import asyncio
import json
import logging
from decimal import Decimal
from types import SimpleNamespace

from adapters import extended_client
from adapters.base import Position
from grid.grid_engine import GridConfig, GridEngine
from grid.grid_state import GridState
from grid.regime import GridMode
from tools.run_grid import _build_parser


def test_request_timeout_is_short() -> None:
    """SDK 默认 500 秒会让一次卡顿冻结整个风控循环。"""
    assert (
        extended_client.build_client_config("mainnet")
        .defaults.request_timeout_seconds
        <= 10
    )


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


def test_position_mark_and_liquidation_are_reused_within_round(tmp_path) -> None:
    """一次持仓响应中的 mark/liq 必须同时供硬止损与 TPSL 使用。"""

    class Ext:
        def __init__(self) -> None:
            self.position_calls = 0
            self.liquidation_calls = 0

        async def get_position(self, market):
            self.position_calls += 1
            return Position(
                market=market,
                signed_size=Decimal("0.01"),
                raw=SimpleNamespace(mark_price="100", liquidation_price="80"),
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
    assert asyncio.run(eng._check_hard_stop(signed_size=inv, mark=mark)) is False
    assert asyncio.run(eng._maintain_tpsl(mark, inv)) is True
    asyncio.run(
        eng._advance_band(
            mark,
            GridMode.NEUTRAL,
            signed_size=inv,
        )
    )

    assert ext.position_calls == 1
    assert ext.liquidation_calls == 0


def test_failed_rounds_increment_counter_and_write_live_snapshot(
    tmp_path,
    monkeypatch,
    caplog,
) -> None:
    """整轮快速失败时也必须把连续失败数写进 live 快照。"""
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
    assert "引擎连续失败 5 轮" in caplog.text


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


def test_polling_defaults_are_ten_seconds() -> None:
    """引擎配置和命令行默认轮询间隔保持一致。"""
    assert GridConfig().poll_interval == 10.0
    assert _build_parser().parse_args([]).interval == 10
