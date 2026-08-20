"""对冲心跳互锁测试。

这些测试锁定方向性门控：互锁只拒绝扩大绝对敞口的挂单，撤单与减仓翻单
必须继续工作，避免重演 2026-08-10 的单向抄底事故。
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from adapters.base import Side
from grid.grid_engine import GridConfig, GridEngine
from grid.regime import GridMode
from tools import run_lighter_mm


class _RecordingAdapter:
    """只记录本地写请求；不访问任何真实网络。"""

    def __init__(self) -> None:
        self.placed: list[dict] = []
        self.cancelled: list[object] = []
        self.open_orders: list[object] = []
        self.history: list[object] = []

    async def place_limit_order(
        self,
        market,
        side,
        amount,
        price,
        *,
        reduce_only=False,
    ):
        self.placed.append(
            {
                "market": market,
                "side": side,
                "amount": Decimal(str(amount)),
                "price": Decimal(str(price)),
                "reduce_only": reduce_only,
            }
        )
        return SimpleNamespace(id=f"new-{len(self.placed)}", status="NEW")

    async def cancel_order(self, _market, order_id) -> None:
        self.cancelled.append(order_id)

    async def get_open_orders(self, _market):
        return self.open_orders

    async def get_orders_history(self, _market, limit=100, **_kwargs):
        del limit
        return self.history


def _write_heartbeat(
    path,
    *,
    ts: float,
    primary_read_ok: bool = True,
    hedge_read_ok: bool = True,
) -> None:
    path.write_text(
        json.dumps(
            {
                "ts": ts,
                "interval": 30.0,
                "primary_read_ok": primary_read_ok,
                "hedge_read_ok": hedge_read_ok,
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )


def _interlocked_engine(monkeypatch, tmp_path, *, now: float = 1_000.0):
    heartbeat = tmp_path / "lighter_hedge.jsonl"
    _write_heartbeat(heartbeat, ts=now - 91.0)
    monkeypatch.setattr("grid.grid_engine.time.time", lambda: now)
    adapter = _RecordingAdapter()
    engine = GridEngine(
        adapter,
        GridConfig(
            dry_run=False,
            unit_usd=10.0,
            max_inventory_usd=10_000.0,
            state_path=str(tmp_path / "grid" / "state.json"),
            hedge_heartbeat_path=str(heartbeat),
            hedge_heartbeat_timeout_s=90.0,
        ),
    )
    engine._refresh_hedge_interlock()
    assert engine.hedge_interlock_active is True
    return engine, adapter


def test_replay_0819_stale_heartbeat_stops_new_positions(
    monkeypatch,
    tmp_path,
) -> None:
    """心跳停在 00:08、做市于 00:10 继续跑时必须拒绝新开仓。"""
    heartbeat_at = datetime(2026, 8, 19, 0, 8, tzinfo=timezone.utc).timestamp()
    now = datetime(2026, 8, 19, 0, 10, tzinfo=timezone.utc).timestamp()
    heartbeat = tmp_path / "lighter_hedge.jsonl"
    _write_heartbeat(heartbeat, ts=heartbeat_at)
    monkeypatch.setattr("grid.grid_engine.time.time", lambda: now)
    adapter = _RecordingAdapter()
    engine = GridEngine(
        adapter,
        GridConfig(
            dry_run=False,
            unit_usd=10.0,
            max_inventory_usd=10_000.0,
            hedge_heartbeat_path=str(heartbeat),
            hedge_heartbeat_timeout_s=90.0,
        ),
    )

    engine._refresh_hedge_interlock()
    placed = asyncio.run(engine._place(100, Side.BUY, 0.0, why="回放补买格"))

    assert engine.hedge_interlock_active is True
    assert "陈旧" in engine.hedge_interlock_reason
    assert placed is None
    assert adapter.placed == []


def test_missing_heartbeat_fails_closed(monkeypatch, tmp_path) -> None:
    """文件不存在不是正常状态，必须失败关闭。"""
    monkeypatch.setattr("grid.grid_engine.time.time", lambda: 1_000.0)
    engine = GridEngine(
        _RecordingAdapter(),
        GridConfig(
            hedge_heartbeat_path=str(tmp_path / "missing.jsonl"),
            hedge_heartbeat_timeout_s=90.0,
        ),
    )

    engine._refresh_hedge_interlock()

    assert engine.hedge_interlock_active is True
    assert "不存在" in engine.hedge_interlock_reason


@pytest.mark.parametrize("last_line", ["{损坏", '{"primary_read_ok": true}'])
def test_corrupt_or_timestamp_less_last_line_fails_closed(
    monkeypatch,
    tmp_path,
    last_line,
) -> None:
    """只看末行；末行损坏或缺时间戳时不得退回更早的健康记录。"""
    heartbeat = tmp_path / "lighter_hedge.jsonl"
    heartbeat.write_text(
        '{"ts": 999, "primary_read_ok": true, "hedge_read_ok": true}\n'
        + last_line
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("grid.grid_engine.time.time", lambda: 1_000.0)
    engine = GridEngine(
        _RecordingAdapter(),
        GridConfig(
            hedge_heartbeat_path=str(heartbeat),
            hedge_heartbeat_timeout_s=90.0,
        ),
    )

    engine._refresh_hedge_interlock()

    assert engine.hedge_interlock_active is True
    assert any(word in engine.hedge_interlock_reason for word in ("损坏", "时间戳"))


@pytest.mark.parametrize(
    ("primary_read_ok", "hedge_read_ok"),
    [(False, True), (True, False)],
)
def test_fresh_heartbeat_with_failed_leg_read_fails_closed(
    monkeypatch,
    tmp_path,
    primary_read_ok,
    hedge_read_ok,
) -> None:
    """进程仍写心跳但任一腿读取失败，也属于对冲未生效。"""
    heartbeat = tmp_path / "lighter_hedge.jsonl"
    _write_heartbeat(
        heartbeat,
        ts=999.0,
        primary_read_ok=primary_read_ok,
        hedge_read_ok=hedge_read_ok,
    )
    monkeypatch.setattr("grid.grid_engine.time.time", lambda: 1_000.0)
    engine = GridEngine(
        _RecordingAdapter(),
        GridConfig(
            hedge_heartbeat_path=str(heartbeat),
            hedge_heartbeat_timeout_s=90.0,
        ),
    )

    engine._refresh_hedge_interlock()

    assert engine.hedge_interlock_active is True
    assert "读取失败" in engine.hedge_interlock_reason


def test_interlock_rejects_order_that_increases_absolute_exposure(
    monkeypatch,
    tmp_path,
) -> None:
    engine, adapter = _interlocked_engine(monkeypatch, tmp_path)

    result = asyncio.run(engine._place(100, Side.BUY, 100.0, why="扩大多头"))

    assert result is None
    assert adapter.placed == []


def test_interlock_allows_order_that_reduces_absolute_exposure(
    monkeypatch,
    tmp_path,
) -> None:
    engine, adapter = _interlocked_engine(monkeypatch, tmp_path)

    result = asyncio.run(engine._place(101, Side.SELL, 100.0, why="缩小多头"))

    assert result is not None
    assert [order["side"] for order in adapter.placed] == [Side.SELL]


def test_interlock_does_not_block_fill_replacement(
    monkeypatch,
    tmp_path,
) -> None:
    """买单成交后的卖出翻单必须真实经过互锁门控并被放行。"""
    engine, adapter = _interlocked_engine(monkeypatch, tmp_path)
    adapter.history = [
        SimpleNamespace(
            id="filled-buy",
            status="FILLED",
            filled_qty=Decimal("0.001"),
            average_price=Decimal("100000"),
        )
    ]
    engine._orders = {100: {"id": "filled-buy", "side": Side.BUY}}

    asyncio.run(engine._handle_fills(inv_usd=100.0))

    assert len(adapter.placed) == 1
    assert adapter.placed[0]["side"] is Side.SELL
    assert 101 in engine._orders


def test_interlock_keeps_cancel_path_available(monkeypatch, tmp_path) -> None:
    engine, adapter = _interlocked_engine(monkeypatch, tmp_path)
    engine._orders = {100: {"id": "existing-buy", "side": Side.BUY}}

    asyncio.run(engine._cancel(100, why="互锁清理扩大敞口挂单"))

    assert adapter.cancelled == ["existing-buy"]
    assert engine._orders == {}


def test_interlock_immediately_cancels_existing_expanding_order(
    monkeypatch,
    tmp_path,
) -> None:
    """不能只挡新单；盘口上会继续扩大多头的既有买单也要走撤单路径。"""
    engine, adapter = _interlocked_engine(monkeypatch, tmp_path)
    engine._orders = {
        100: {
            "id": "existing-buy",
            "side": Side.BUY,
            "qty": Decimal("0.0001"),
        },
        101: {
            "id": "existing-sell",
            "side": Side.SELL,
            "qty": Decimal("0.0001"),
        },
    }

    asyncio.run(engine._cancel_interlock_expanding_orders(inv_usd=100.0))

    assert adapter.cancelled == ["existing-buy"]
    assert set(engine._orders) == {101}


@pytest.mark.parametrize("side", [Side.BUY, Side.SELL])
def test_interlock_rejects_both_sides_when_inventory_is_zero(
    monkeypatch,
    tmp_path,
    side,
) -> None:
    engine, adapter = _interlocked_engine(monkeypatch, tmp_path)

    result = asyncio.run(engine._place(100, side, 0.0, why="零库存开仓"))

    assert result is None
    assert adapter.placed == []


def test_interlock_logs_only_trigger_and_recovery_at_warning(
    monkeypatch,
    tmp_path,
    caplog,
) -> None:
    heartbeat = tmp_path / "lighter_hedge.jsonl"
    _write_heartbeat(heartbeat, ts=900.0)
    monkeypatch.setattr("grid.grid_engine.time.time", lambda: 1_000.0)
    engine = GridEngine(
        _RecordingAdapter(),
        GridConfig(
            hedge_heartbeat_path=str(heartbeat),
            hedge_heartbeat_timeout_s=90.0,
        ),
    )

    with caplog.at_level(logging.DEBUG, logger="grid_engine"):
        engine._refresh_hedge_interlock()
        engine._refresh_hedge_interlock()
        _write_heartbeat(heartbeat, ts=999.0)
        engine._refresh_hedge_interlock()
        engine._refresh_hedge_interlock()

    assert engine.hedge_interlock_active is False
    warnings = [
        record.getMessage()
        for record in caplog.records
        if record.levelno >= logging.WARNING and "对冲互锁" in record.getMessage()
    ]
    assert len(warnings) == 2
    assert "触发" in warnings[0]
    assert "恢复" in warnings[1]


def test_interlock_state_is_visible_in_heartbeat_and_live_status(
    monkeypatch,
    tmp_path,
) -> None:
    engine, _adapter = _interlocked_engine(monkeypatch, tmp_path)
    args = SimpleNamespace(
        market="BTC",
        live=False,
        levels=4,
        unit=50.0,
        max_inv=500.0,
        interval=2.5,
    )

    heartbeat = run_lighter_mm._heartbeat_payload(engine, args, success=True)
    asyncio.run(
        engine._dump_live(
            mark=None,
            mode=GridMode.NEUTRAL,
            inv=Decimal("0"),
            closes=[],
        )
    )
    live = json.loads(
        (tmp_path / "grid" / "grid_live.json").read_text(encoding="utf-8")
    )

    assert heartbeat["hedge_interlock_active"] is True
    assert heartbeat["hedge_interlock_reason"]
    assert live["hedge_interlock_active"] is True
    assert live["hedge_interlock_reason"]


def test_partial_interlock_configuration_fails_risk_selfcheck(tmp_path) -> None:
    """声明对冲模式却缺超时参数时，必须沿用风控自检拒绝启动。"""
    engine = GridEngine(
        _RecordingAdapter(),
        GridConfig(hedge_heartbeat_path=str(tmp_path / "lighter_hedge.jsonl")),
    )

    with pytest.raises(RuntimeError, match="对冲存活互锁.*超时"):
        engine.validate_risk_controls()


def test_unconfigured_interlock_does_not_read_file_or_change_order_call(
    monkeypatch,
) -> None:
    adapter = _RecordingAdapter()
    engine = GridEngine(
        adapter,
        GridConfig(
            dry_run=False,
            unit_usd=10.0,
            max_inventory_usd=10_000.0,
        ),
    )

    def fail_read(*_args, **_kwargs):
        pytest.fail("未配置互锁时不得读取任何心跳文件")

    monkeypatch.setattr("grid.grid_engine.Path.read_text", fail_read)
    engine._refresh_hedge_interlock()
    result = asyncio.run(engine._place(100, Side.BUY, 0.0, why="兼容路径"))

    assert engine.hedge_interlock_active is False
    assert result is not None
    assert adapter.placed == [
        {
            "market": "BTC-USD",
            "side": Side.BUY,
            "amount": adapter.placed[0]["amount"],
            "price": adapter.placed[0]["price"],
            "reduce_only": False,
        }
    ]
