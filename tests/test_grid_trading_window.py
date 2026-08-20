"""网格交易时段窗口的离线回归测试。"""

from __future__ import annotations

import asyncio
import logging
import os
import time as system_time
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

from adapters.base import MarketPrice, Position, Side
from grid.grid_engine import GridConfig, GridEngine
from grid.grid_state import GridState
from tools import run_grid, run_lighter_mm


class _ForbiddenCandles:
    """窗口外不得触碰的行情源。"""

    async def get_hourly_candles(self, _market: str, _limit: int):
        raise AssertionError("计划停机期间不得读取 K 线或进入铺网格路径")


class _WindowAdapter:
    """同时覆盖成功、部分成交、异常与持续残仓的交易适配器桩。"""

    def __init__(
        self,
        position: Decimal | str = Decimal("0"),
        *,
        maker_fills: list[Decimal | tuple[Decimal, str]] | None = None,
        market_results: list[Decimal | Exception] | None = None,
    ) -> None:
        self.position = Decimal(str(position))
        self.events: list[str] = []
        self.limit_orders: list[dict] = []
        self.market_orders: list[dict] = []
        self.cancelled_ids: list[str] = []
        self._maker_fills = list(maker_fills or [Decimal("0")])
        self._market_results = list(market_results or [Decimal("1")])
        self._last_reported_fill = Decimal("0")

    async def connect(self) -> None:
        self.events.append("连接")

    async def get_position(self, market: str) -> Position:
        return Position(market, self.position)

    async def get_mark_price(self, _market: str) -> Decimal:
        return Decimal("100")

    async def get_market_price(self, market: str) -> MarketPrice:
        return MarketPrice(market, Decimal("99"), Decimal("101"))

    async def get_open_orders(self, _market: str) -> list:
        return []

    async def cancel_grid_orders(self, _market: str) -> int:
        self.events.append("撤单")
        return 0

    async def cancel_tpsl(self, _market: str) -> None:
        self.events.append("撤TPSL")

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
        order = {
            "market": market,
            "side": side,
            "amount": Decimal(str(amount)),
            "price": Decimal(str(price)),
            "post_only": post_only,
            "reduce_only": reduce_only,
        }
        self.limit_orders.append(order)
        self.events.append("maker平仓" if reduce_only else "扩大敞口")
        return SimpleNamespace(data=SimpleNamespace(id=f"maker-{len(self.limit_orders)}"))

    async def get_order_by_id(self, _market: str, order_id: str):
        value = self._maker_fills[0] if len(self._maker_fills) == 1 else self._maker_fills.pop(0)
        if isinstance(value, tuple):
            filled, status = value
        else:
            filled = Decimal(str(value))
            status = "FILLED" if filled >= self.limit_orders[-1]["amount"] else "NEW"
        filled = Decimal(str(filled))
        delta = max(Decimal("0"), filled - self._last_reported_fill)
        self._apply_fill(self.limit_orders[-1]["side"], delta)
        self._last_reported_fill = filled
        return SimpleNamespace(id=order_id, filled_qty=filled, status=status)

    async def cancel_order(self, _market: str, _order_id: str) -> None:
        self.cancelled_ids.append(_order_id)
        self.events.append("撤maker")

    async def market_order(
        self,
        market: str,
        side: Side,
        amount: Decimal,
        *,
        reduce_only: bool = False,
    ):
        result = self._market_results[0] if len(self._market_results) == 1 else self._market_results.pop(0)
        if isinstance(result, Exception):
            self.events.append("市价失败")
            raise result
        filled = Decimal(str(amount)) * Decimal(str(result))
        self._apply_fill(side, filled)
        order = {
            "market": market,
            "side": side,
            "amount": Decimal(str(amount)),
            "filled": filled,
            "reduce_only": reduce_only,
        }
        self.market_orders.append(order)
        self.events.append("市价平仓")
        return SimpleNamespace(data=SimpleNamespace(id=f"market-{len(self.market_orders)}"))

    def _apply_fill(self, side: Side, amount: Decimal) -> None:
        signed = amount if side is Side.BUY else -amount
        updated = self.position + signed
        if self.position != 0 and self.position * updated < 0:
            updated = Decimal("0")
        self.position = updated


def _utc(year: int, month: int, day: int, hour: int, minute: int = 0) -> datetime:
    """构造与本机时区无关的 UTC 测试时刻。"""
    return datetime(year, month, day, hour, minute, tzinfo=timezone.utc)


def _window_engine(
    adapter: _WindowAdapter,
    now: list[datetime],
    *,
    start: str = "05:00",
    end: str = "20:00",
    maker_timeout: float = 0.0,
) -> GridEngine:
    """在 RED 阶段用动态字段描述待实现配置，避免测试收集失败。"""
    config = GridConfig(
        dry_run=False,
        trend_aware=False,
        exchange_tpsl=False,
        hard_stop_dist=0,
        max_drawdown_pct=0,
        max_inventory_usd=1000,
        flat_confirmation_interval=0,
    )
    config.trading_window_start = start
    config.trading_window_end = end
    config.maker_first_timeout_s = maker_timeout
    engine = GridEngine(adapter, config, candle_source=_ForbiddenCandles())
    engine._clock = lambda: now[0]
    return engine


def _assert_window_api(engine: GridEngine) -> None:
    """让 RED 结果明确指向缺失的窗口能力。"""
    assert hasattr(engine, "_is_trading_window_open"), "尚未实现 UTC+8 窗口判定"


def test_local_timezone_does_not_change_utc_plus_8_decision(monkeypatch) -> None:
    """1.1：本机切到纽约时区后，UTC+8 的 09:00 仍为窗口内。"""
    previous_tz = os.environ.get("TZ")
    monkeypatch.setenv("TZ", "America/New_York")
    if hasattr(system_time, "tzset"):
        system_time.tzset()
    try:
        now = [_utc(2026, 8, 20, 1)]
        engine = _window_engine(_WindowAdapter(), now)
        _assert_window_api(engine)
        assert engine._is_trading_window_open() is True
    finally:
        if previous_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = previous_tz
        if hasattr(system_time, "tzset"):
            system_time.tzset()


def test_window_boundaries_are_start_inclusive_end_exclusive() -> None:
    """1.2：05:00 归窗口内，20:00 归停机，19:59 仍可交易。"""
    now = [_utc(2026, 8, 19, 21)]  # UTC+8 次日 05:00
    engine = _window_engine(_WindowAdapter(), now)
    _assert_window_api(engine)

    assert engine._is_trading_window_open() is True
    now[0] = _utc(2026, 8, 20, 11, 59)
    assert engine._is_trading_window_open() is True
    now[0] = _utc(2026, 8, 20, 12)
    assert engine._is_trading_window_open() is False


def test_cross_midnight_window_decision() -> None:
    """1.3：20:00–05:00 跨日窗口在两段夜间均为窗口内。"""
    now = [_utc(2026, 8, 20, 14)]  # UTC+8 22:00
    engine = _window_engine(_WindowAdapter(), now, start="20:00", end="05:00")
    _assert_window_api(engine)

    assert engine._is_trading_window_open() is True
    now[0] = _utc(2026, 8, 20, 19)  # UTC+8 次日 03:00
    assert engine._is_trading_window_open() is True
    now[0] = _utc(2026, 8, 20, 21)  # UTC+8 次日 05:00
    assert engine._is_trading_window_open() is False
    now[0] = _utc(2026, 8, 20, 4)  # UTC+8 12:00
    assert engine._is_trading_window_open() is False


def test_weekend_and_weekday_use_identical_window() -> None:
    """1.4：周六、周日与周一同一时刻的结果完全一致。"""
    now = [_utc(2026, 8, 22, 1)]  # 周六 UTC+8 09:00
    engine = _window_engine(_WindowAdapter(), now)
    _assert_window_api(engine)

    decisions = []
    for day in (22, 23, 24):
        now[0] = _utc(2026, 8, day, 1)
        decisions.append(engine._is_trading_window_open())
    assert decisions == [True, True, True]


def test_scheduled_stop_cancels_grid_before_closing_inventory() -> None:
    """1.5：进入停机后，第一笔平仓委托之前必须已完成撤网格单。"""
    adapter = _WindowAdapter("-1")
    engine = _window_engine(adapter, [_utc(2026, 8, 20, 12)])

    asyncio.run(engine.run_once())

    assert adapter.events.index("撤单") < adapter.events.index("maker平仓")
    assert adapter.position == 0


def test_scheduled_stop_never_places_exposure_increasing_order() -> None:
    """1.6：停机轮次只允许 reduce-only 平仓，不得产生扩大敞口委托。"""
    adapter = _WindowAdapter("-1")
    engine = _window_engine(adapter, [_utc(2026, 8, 20, 12)])

    asyncio.run(engine.run_once())

    assert "扩大敞口" not in adapter.events
    assert adapter.limit_orders
    assert all(order["reduce_only"] for order in adapter.limit_orders)
    assert all(order["reduce_only"] for order in adapter.market_orders)


def test_zero_inventory_scheduled_stop_places_no_close_order() -> None:
    """1.7：库存已为零时只撤单，不生成 maker 或市价平仓单。"""
    adapter = _WindowAdapter("0")
    engine = _window_engine(adapter, [_utc(2026, 8, 20, 12)])

    asyncio.run(engine.run_once())

    assert adapter.events[0] == "撤单"
    assert adapter.limit_orders == []
    assert adapter.market_orders == []


def test_maker_timeout_cancels_then_markets_only_partial_remainder() -> None:
    """1.8：maker 部分成交后先撤单，再以市价补齐剩余量。"""
    adapter = _WindowAdapter("-1", maker_fills=[Decimal("0.4")])
    engine = _window_engine(adapter, [_utc(2026, 8, 20, 12)])

    asyncio.run(engine.run_once())

    assert adapter.events.index("撤maker") < adapter.events.index("市价平仓")
    assert adapter.market_orders[0]["amount"] == Decimal("0.6")
    assert adapter.position == 0


def test_zero_maker_fill_still_markets_full_inventory() -> None:
    """1.9：maker 完全未成交也必须转市价，不得带仓进入停机。"""
    adapter = _WindowAdapter("-1", maker_fills=[Decimal("0")])
    engine = _window_engine(adapter, [_utc(2026, 8, 20, 12)])

    asyncio.run(engine.run_once())

    assert adapter.market_orders[0]["amount"] == Decimal("1")
    assert adapter.position == 0


def test_close_exception_alerts_and_retries_next_round(caplog) -> None:
    """1.10：首轮市价平仓异常要告警，第二轮仍继续尝试。"""
    adapter = _WindowAdapter(
        "-1",
        maker_fills=[Decimal("0"), Decimal("0")],
        market_results=[RuntimeError("模拟平仓失败"), Decimal("1")],
    )
    engine = _window_engine(adapter, [_utc(2026, 8, 20, 12)])

    with caplog.at_level(logging.DEBUG, logger="grid_engine"):
        asyncio.run(engine.run_once())
        assert adapter.position == Decimal("-1")
        asyncio.run(engine.run_once())

    assert adapter.events.count("市价失败") == 1
    assert adapter.events.count("市价平仓") == 1
    assert adapter.position == 0
    assert "下轮继续重试" in caplog.text


def test_nonzero_inventory_is_retried_each_stopped_round_until_flat() -> None:
    """1.11：每轮复核真实库存，部分平仓后持续重试直至归零。"""
    adapter = _WindowAdapter(
        "-1",
        maker_fills=[Decimal("0"), Decimal("0")],
        market_results=[Decimal("0.5"), Decimal("1")],
    )
    engine = _window_engine(adapter, [_utc(2026, 8, 20, 12)])

    asyncio.run(engine.run_once())
    assert adapter.position == Decimal("-0.5")
    asyncio.run(engine.run_once())

    assert adapter.position == 0
    assert len(adapter.market_orders) == 2


def test_planned_stop_round_still_emits_identifiable_heartbeat(monkeypatch) -> None:
    """1.12：停机轮次照常写心跳，并明确标记为计划内停机。"""
    payloads: list[dict] = []
    adapter = _WindowAdapter("0")
    engine = _window_engine(adapter, [_utc(2026, 8, 20, 12)])
    args = SimpleNamespace(
        market="BTC",
        live=True,
        levels=10,
        unit=300,
        max_inv=3750,
        interval=2.5,
    )
    monkeypatch.setattr(run_lighter_mm, "_append_heartbeat", payloads.append)
    run_lighter_mm._install_heartbeat(engine, args)

    asyncio.run(engine.run_once())

    assert payloads[-1]["success"] is True
    assert payloads[-1]["trading_window_state"] == "planned_stop"
    assert payloads[-1]["planned_stop"] is True


def test_window_reentry_resumes_active_grid_without_restart(monkeypatch) -> None:
    """1.13：同一引擎跨入窗口后自动执行正常铺网格路径。"""
    now = [_utc(2026, 8, 20, 12)]
    adapter = _WindowAdapter("0")
    engine = _window_engine(adapter, now)
    engine.config.trend_aware = True
    active_calls: list[bool] = []

    async def no_market_snapshot() -> None:
        return None

    async def active_cycle(include_slow: bool = True) -> str:
        active_calls.append(include_slow)
        return "已恢复铺网格"

    monkeypatch.setattr(engine, "_refresh_market_price", no_market_snapshot)
    monkeypatch.setattr(engine, "_run_once_trend_aware", active_cycle)

    asyncio.run(engine.run_once())
    now[0] = _utc(2026, 8, 20, 1)  # UTC+8 09:00
    result = asyncio.run(engine.run_once(include_slow=False))

    assert active_calls == [False]
    assert result == "已恢复铺网格"


def test_starting_outside_window_adopts_then_flattens_without_grid_orders() -> None:
    """1.14：停机时段启动可接管库存，但首轮不铺网格并将其平零。"""
    adapter = _WindowAdapter("-1")
    engine = _window_engine(adapter, [_utc(2026, 8, 20, 12)])

    asyncio.run(engine.connect())
    asyncio.run(engine.run_once())

    assert adapter.events[0] == "连接"
    assert "扩大敞口" not in adapter.events
    assert adapter.position == 0


def test_scheduled_flatten_does_not_write_halted(tmp_path) -> None:
    """1.15：计划离场不持久化 kill switch，原状态仍可自动恢复。"""
    adapter = _WindowAdapter("-1")
    engine = _window_engine(adapter, [_utc(2026, 8, 20, 12)])
    engine.config.state_path = str(tmp_path / "grid_state.json")
    engine._state = GridState(90.0, 110.0, False, None, False)

    asyncio.run(engine.run_once())

    assert engine._state.halted is False
    assert not (tmp_path / "grid_state.json").exists()


def test_unconfigured_window_preserves_active_round_without_reading_clock(monkeypatch) -> None:
    """1.16：未配置窗口时不判时、不撤单，逐轮进入原有执行路径。"""
    config = GridConfig(trend_aware=True)
    assert hasattr(config, "trading_window_start"), "尚未实现默认关闭的窗口配置"
    assert config.trading_window_start is None
    assert config.trading_window_end is None
    adapter = _WindowAdapter("0")
    engine = GridEngine(adapter, config, candle_source=_ForbiddenCandles())
    engine._clock = lambda: (_ for _ in ()).throw(AssertionError("未配置时不得读取时钟"))
    active_calls: list[bool] = []

    async def no_market_snapshot() -> None:
        return None

    async def active_cycle(include_slow: bool = True) -> str:
        active_calls.append(include_slow)
        return "原行为"

    monkeypatch.setattr(engine, "_refresh_market_price", no_market_snapshot)
    monkeypatch.setattr(engine, "_run_once_trend_aware", active_cycle)

    result = asyncio.run(engine.run_once(include_slow=False))

    assert result == "原行为"
    assert active_calls == [False]
    assert adapter.events == []


def test_full_maker_fill_never_places_taker_close() -> None:
    """补足 spec 场景：maker 在超时前全成时不得重复吃单。"""
    adapter = _WindowAdapter("-1", maker_fills=[Decimal("1")])
    engine = _window_engine(
        adapter,
        [_utc(2026, 8, 20, 12)],
        maker_timeout=0.01,
    )

    asyncio.run(engine.run_once())

    assert adapter.position == 0
    assert adapter.market_orders == []
    assert "撤maker" not in adapter.events


def test_scheduled_stop_cancels_tracked_reduce_only_grid_order_first() -> None:
    """恢复阶梯也是网格挂单，离场时必须在平仓前逐单撤掉。"""
    adapter = _WindowAdapter("-1")
    engine = _window_engine(adapter, [_utc(2026, 8, 20, 12)])
    engine._orders = {
        558: {
            "id": "recovery-1",
            "side": Side.BUY,
            "reduce_only": True,
            "qty": Decimal("1"),
        }
    }

    asyncio.run(engine.run_once())

    assert adapter.cancelled_ids[0] == "recovery-1"
    assert adapter.events.index("撤maker") < adapter.events.index("maker平仓")


def test_scheduled_stop_dry_run_performs_no_exchange_write() -> None:
    """窗口启用不能破坏 dry-run：撤单、maker、市价与 TPSL 写操作都禁止。"""
    adapter = _WindowAdapter("-1")
    engine = _window_engine(adapter, [_utc(2026, 8, 20, 12)])
    engine.config.dry_run = True
    engine._orders = {
        558: {"id": "existing-1", "side": Side.BUY, "reduce_only": False}
    }

    asyncio.run(engine.run_once())

    assert adapter.events == []
    assert adapter.cancelled_ids == []
    assert adapter.limit_orders == []
    assert adapter.market_orders == []


def test_august_19_replay_stops_before_3666_point_night_rally() -> None:
    """4.3：20:00 离场后，20–23 点累计 +3,666 的涨幅不再作用于网格库存。"""
    now = [_utc(2026, 8, 19, 11)]  # UTC+8 19:00
    engine = _window_engine(_WindowAdapter(), now)
    _assert_window_api(engine)
    hourly_rally = {20: 386, 21: 124, 22: 918, 23: 2238}

    now[0] = _utc(2026, 8, 19, 12)  # UTC+8 20:00
    assert engine._is_trading_window_open() is False
    avoided_points = sum(
        move
        for hour, move in hourly_rally.items()
        if not engine._is_trading_window_open(
            _utc(2026, 8, 19, hour - 8)
        )
    )

    assert avoided_points == 3666


def test_extended_cli_defaults_disabled_and_wires_window_arguments() -> None:
    """3.1 / 4.4：Extended 默认不启用，显式边界与 maker 超时完整透传。"""
    defaults = run_grid._build_parser().parse_args([])
    enabled = run_grid._build_parser().parse_args(
        [
            "--trading-window-start",
            "05:00",
            "--trading-window-end",
            "20:00",
            "--maker-first-timeout",
            "15",
        ]
    )

    default_config = run_grid._grid_config(defaults)
    enabled_config = run_grid._grid_config(enabled)
    assert (default_config.trading_window_start, default_config.trading_window_end) == (
        None,
        None,
    )
    assert (
        enabled_config.trading_window_start,
        enabled_config.trading_window_end,
        enabled_config.maker_first_timeout_s,
    ) == ("05:00", "20:00", 15.0)


def test_lighter_cli_defaults_disabled_and_wires_window_arguments() -> None:
    """3.2 / 4.4：Lighter 默认不启用，显式边界与 maker 超时完整透传。"""
    defaults = run_lighter_mm.build_parser().parse_args([])
    enabled = run_lighter_mm.build_parser().parse_args(
        [
            "--trading-window-start",
            "05:00",
            "--trading-window-end",
            "20:00",
            "--maker-first-timeout",
            "15",
        ]
    )

    default_config = run_lighter_mm._grid_config(defaults)
    enabled_config = run_lighter_mm._grid_config(enabled)
    assert (default_config.trading_window_start, default_config.trading_window_end) == (
        None,
        None,
    )
    assert (
        enabled_config.trading_window_start,
        enabled_config.trading_window_end,
        enabled_config.maker_first_timeout_s,
    ) == ("05:00", "20:00", 15.0)


def test_extended_startup_summary_includes_window_state(monkeypatch, capsys) -> None:
    """3.3：Extended 启动摘要显示窗口边界、固定时区与当前状态。"""
    summaries: list[str] = []

    class _Client:
        async def close(self) -> None:
            return None

    class _Engine:
        def __init__(self, _ext, config, **_kwargs) -> None:
            self.config = config

        def trading_window_summary(self) -> str:
            summaries.append("called")
            return "UTC+8 05:00–20:00，当前=窗口内，maker 超时=15秒"

        async def run_forever(self) -> None:
            return None

    monkeypatch.setattr(run_grid.ExtendedClient, "from_env", lambda **_kwargs: _Client())
    monkeypatch.setattr(run_grid, "GridEngine", _Engine)
    args = run_grid._build_parser().parse_args(
        ["--trading-window-start", "05:00", "--trading-window-end", "20:00"]
    )

    asyncio.run(run_grid._main(args))

    assert summaries == ["called"]
    assert "交易窗口=UTC+8 05:00–20:00，当前=窗口内" in capsys.readouterr().out


def test_lighter_startup_summary_includes_window_state(
    monkeypatch,
    caplog,
    tmp_path,
) -> None:
    """3.3：Lighter 启动日志同样展示计划时段状态。"""
    summaries: list[str] = []

    class _Client:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        @classmethod
        def from_env(cls, *_args, **_kwargs):
            return cls()

        async def close(self) -> None:
            return None

    class _Engine:
        def __init__(self, _ext, config, **_kwargs) -> None:
            self.config = config

        def trading_window_summary(self) -> str:
            summaries.append("called")
            return "UTC+8 05:00–20:00，当前=计划停机，maker 超时=15秒"

        async def run_forever(self) -> None:
            return None

    monkeypatch.setattr(run_lighter_mm, "LighterClient", _Client)
    monkeypatch.setattr(run_lighter_mm, "ExtendedClient", _Client)
    monkeypatch.setattr(run_lighter_mm, "GridEngine", _Engine)
    args = run_lighter_mm.build_parser().parse_args(
        [
            "--lighter-address",
            "0xabc",
            "--state-path",
            str(tmp_path / "lighter" / "state.json"),
            "--trading-window-start",
            "05:00",
            "--trading-window-end",
            "20:00",
        ]
    )

    with caplog.at_level(logging.INFO, logger="lighter_mm"):
        asyncio.run(run_lighter_mm._main(args))

    assert summaries == ["called"]
    assert "交易窗口=UTC+8 05:00–20:00，当前=计划停机" in caplog.text
