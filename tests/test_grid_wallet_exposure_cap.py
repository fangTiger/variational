"""网格按权益比例限制库存上限的离线测试。"""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

from adapters.base import Side
from grid import grid_engine
from grid.grid_engine import GridConfig, GridEngine, GridMode
from tools import run_grid, run_lighter_mm


class _BalanceAdapter:
    """可计数、可失败、可返回无效权益的适配器桩。"""

    def __init__(self, *results) -> None:
        self.results = list(results)
        self.balance_calls = 0
        self.market_orders: list[tuple] = []
        self.cancel_calls: list[str] = []

    async def get_balance(self):
        self.balance_calls += 1
        result = self.results[min(self.balance_calls - 1, len(self.results) - 1)]
        if isinstance(result, Exception):
            raise result
        return SimpleNamespace(equity=result)

    async def market_order(self, *args, **kwargs):
        self.market_orders.append((args, kwargs))
        return "unexpected-market-order"

    async def cancel_grid_orders(self, market: str) -> None:
        self.cancel_calls.append(market)


def _engine(adapter: _BalanceAdapter, *, ratio: float | None = 3.0) -> GridEngine:
    """构造只开启本次比例上限、不会触发其他风控动作的引擎。"""
    config = GridConfig(
        dry_run=False,
        unit_usd=100.0,
        max_inventory_usd=3750.0,
        max_drawdown_pct=0,
        exchange_tpsl=False,
        hard_stop_dist=0,
        trend_aware=False,
    )
    # RED 阶段允许现有 GridConfig 被构造，再由断言暴露新字段/行为缺失。
    config.wallet_exposure_ratio = ratio
    return GridEngine(adapter, config)


def _refresh(engine: GridEngine) -> None:
    """通过既有熔断检查入口刷新共享权益缓存。"""
    assert asyncio.run(engine._check_equity_drawdown()) is False


def test_balance_query_failure_falls_back_to_hard_cap(caplog) -> None:
    """权益查询抛错时仍以绝对硬顶拒绝超额挂单。"""
    adapter = _BalanceAdapter(RuntimeError("模拟权益接口失败"))
    engine = _engine(adapter)

    with caplog.at_level(logging.DEBUG, logger="grid_engine"):
        _refresh(engine)
        allowed = engine._within_cap(Side.BUY, inv_usd=3700.0)

    assert allowed is False
    assert adapter.balance_calls == 1
    assert "比例保护当前未生效" in caplog.text
    assert "绝对硬顶" in caplog.text


def test_invalid_equity_values_fall_back_to_hard_cap(caplog) -> None:
    """权益为零、负数或空值时都退回硬顶并告警。"""
    for invalid_equity in (0, -1, None):
        adapter = _BalanceAdapter(invalid_equity)
        engine = _engine(adapter)

        with caplog.at_level(logging.WARNING, logger="grid_engine"):
            _refresh(engine)
            cap, source = engine._effective_inventory_cap()

        assert cap == 3750.0
        assert "绝对硬顶" in source
        assert adapter.balance_calls == 1

    warnings = [
        record
        for record in caplog.records
        if record.levelno == logging.WARNING
        and "比例保护当前未生效" in record.getMessage()
    ]
    assert len(warnings) == 3


def test_equity_fallback_warning_is_rate_limited(caplog) -> None:
    """同一权益失败状态反复计算上限时不线性输出 WARNING。"""
    adapter = _BalanceAdapter(RuntimeError("模拟持续失败"))
    engine = _engine(adapter)

    with caplog.at_level(logging.DEBUG, logger="grid_engine"):
        _refresh(engine)
        for _ in range(5):
            engine._effective_inventory_cap()

    warnings = [
        record
        for record in caplog.records
        if record.levelno == logging.WARNING
        and "比例保护当前未生效" in record.getMessage()
    ]
    assert len(warnings) == 1
    assert adapter.balance_calls == 1


def test_equity_fallback_warns_again_after_recovery(caplog) -> None:
    """比例保护恢复后再次失效属于新状态，必须重新输出 WARNING。"""
    adapter = _BalanceAdapter(
        RuntimeError("第一次失败"),
        730,
        RuntimeError("恢复后再次失败"),
    )
    engine = _engine(adapter)

    with caplog.at_level(logging.DEBUG, logger="grid_engine"):
        _refresh(engine)
        engine._last_equity_check_ts = 0.0
        _refresh(engine)
        engine._last_equity_check_ts = 0.0
        _refresh(engine)

    warnings = [
        record
        for record in caplog.records
        if record.levelno == logging.WARNING
        and "比例保护当前未生效" in record.getMessage()
    ]
    assert len(warnings) == 2


def test_absolute_hard_cap_wins_when_ratio_cap_is_higher() -> None:
    """权益比例值高于硬顶时不得突破绝对硬顶。"""
    engine = _engine(_BalanceAdapter(2000))

    _refresh(engine)
    cap, source = engine._effective_inventory_cap()

    assert cap == 3750.0
    assert "绝对硬顶" in source


def test_wallet_ratio_cap_wins_when_lower_than_hard_cap() -> None:
    """权益比例值更紧时使用权益乘比例的结果。"""
    engine = _engine(_BalanceAdapter(730))

    _refresh(engine)
    cap, source = engine._effective_inventory_cap()

    assert cap == 2190.0
    assert "权益比例" in source


def test_equity_drop_tightens_cap_after_cache_refresh(monkeypatch) -> None:
    """跨过限频周期后权益下降会自动收紧上限。"""
    adapter = _BalanceAdapter(1000, 700)
    engine = _engine(adapter)
    timestamps = iter((1000.0, 1061.0))
    monkeypatch.setattr(grid_engine.time, "time", lambda: next(timestamps))

    _refresh(engine)
    first_cap, _ = engine._effective_inventory_cap()
    _refresh(engine)
    second_cap, _ = engine._effective_inventory_cap()

    assert first_cap == 3000.0
    assert second_cap == 2100.0
    assert second_cap < first_cap


def test_legacy_round_refreshes_ratio_cap_after_interval(monkeypatch) -> None:
    """默认 legacy 轮次也必须刷新共享缓存，不能永久沿用启动权益。"""
    adapter = _BalanceAdapter(1000, 700)
    engine = _engine(adapter)
    now = [1000.0]
    monkeypatch.setattr(grid_engine.time, "time", lambda: now[0])

    async def no_market_snapshot() -> None:
        return None

    async def fake_inventory():
        return 0, 100.0

    async def no_fills(_inv_usd: float) -> None:
        return None

    async def ladder_done(_price: float, _inv_usd: float) -> str:
        return "完成"

    class _Candles:
        async def get_hourly_candles(self, _market: str, _limit: int):
            candle = SimpleNamespace(high=101, low=99, close=100)
            return [candle]

    monkeypatch.setattr(engine, "_refresh_market_price", no_market_snapshot)
    monkeypatch.setattr(engine, "_inv", fake_inventory)
    monkeypatch.setattr(engine, "_handle_fills", no_fills)
    monkeypatch.setattr(engine, "_maintain_ladder", ladder_done)
    monkeypatch.setattr(grid_engine, "adx", lambda *_args, **_kwargs: [1.0])
    monkeypatch.setattr(
        grid_engine,
        "donchian_prev",
        lambda *_args, **_kwargs: ([101.0], [99.0]),
    )
    monkeypatch.setattr(
        grid_engine,
        "decide_mode",
        lambda *_args, **_kwargs: GridMode.NEUTRAL,
    )
    engine._candles = _Candles()

    _refresh(engine)
    first_cap, _ = engine._effective_inventory_cap()
    now[0] = 1061.0
    asyncio.run(engine.run_once())
    second_cap, _ = engine._effective_inventory_cap()

    assert adapter.balance_calls == 2
    assert first_cap == 3000.0
    assert second_cap == 2100.0


def test_existing_over_cap_inventory_rejects_same_direction_order() -> None:
    """已有多头超过比例上限时拒绝继续新增买单。"""
    engine = _engine(_BalanceAdapter(730))

    _refresh(engine)

    assert engine._within_cap(Side.BUY, inv_usd=2300.0) is False


def test_over_cap_rejection_never_calls_close_or_reduce_methods(caplog) -> None:
    """库存超限只拒单，不撤单、不市价平仓也不生成减仓动作。"""
    adapter = _BalanceAdapter(730)
    engine = _engine(adapter)

    _refresh(engine)
    assert engine._within_cap(Side.BUY, inv_usd=2300.0) is False
    with caplog.at_level(logging.INFO, logger="grid_engine"):
        result = asyncio.run(
            engine._place(558, Side.BUY, inv_usd=2300.0, why="比例超限")
        )

    assert result is None
    assert adapter.market_orders == []
    assert adapter.cancel_calls == []
    assert engine._orders == {}
    assert "上限来源=权益比例" in caplog.text


def test_over_cap_inventory_allows_opposite_order_that_reduces_exposure() -> None:
    """已有多头超限时卖单仍放行，保留网格自然回收能力。"""
    adapter = _BalanceAdapter(730)
    engine = _engine(adapter)
    engine.config.dry_run = True

    _refresh(engine)
    result = asyncio.run(
        engine._place(558, Side.SELL, inv_usd=2300.0, why="回收库存")
    )

    assert result is not None
    assert engine._orders[558]["side"] is Side.SELL
    assert adapter.balance_calls == 1


def test_unconfigured_ratio_preserves_absolute_cap_behavior() -> None:
    """不配置比例时字段默认关闭，逐单仍只按绝对硬顶判断。"""
    adapter = _BalanceAdapter(RuntimeError("未配置时不应查询权益"))
    engine = _engine(adapter, ratio=None)

    assert GridConfig().wallet_exposure_ratio is None
    assert engine._within_cap(Side.BUY, inv_usd=3600.0) is True
    assert engine._within_cap(Side.BUY, inv_usd=3700.0) is False
    assert adapter.balance_calls == 0


def test_repeated_cap_calculations_do_not_add_balance_queries() -> None:
    """同一限频周期内多次判断上限只消费一次共享缓存查询。"""
    adapter = _BalanceAdapter(730)
    engine = _engine(adapter)

    _refresh(engine)
    results = [
        engine._within_cap(Side.BUY, inv_usd=2100.0)
        for _ in range(5)
    ]

    assert results == [False] * 5
    assert adapter.balance_calls == 1


def test_august_19_lighter_replay_caps_inventory_near_2189() -> None:
    """回放 8 月 19 日 Lighter 权益，3x 上限应约为 2189 美元。"""
    engine = _engine(_BalanceAdapter(729.76), ratio=3.0)

    _refresh(engine)
    cap, source = engine._effective_inventory_cap()

    assert cap == 2189.28
    assert cap < 3750.0 * 0.6
    assert "权益比例" in source


def test_august_19_short_inventory_rejects_sell_but_allows_buy() -> None:
    """8 月 19 日确切空头场景：只拒绝继续做空，保留回补通道。"""
    adapter = _BalanceAdapter(729.76)
    engine = _engine(adapter, ratio=3.0)
    engine.config.dry_run = True

    _refresh(engine)

    assert engine._within_cap(Side.SELL, inv_usd=-2200.0) is False
    buy_result = asyncio.run(
        engine._place(558, Side.BUY, inv_usd=-2200.0, why="空头回补")
    )
    assert buy_result is not None
    assert engine._orders[558]["side"] is Side.BUY


def test_run_forever_logs_current_effective_cap_from_shared_cache(
    monkeypatch,
    caplog,
) -> None:
    """启动摘要使用首次共享权益缓存，显示当前生效值及来源。"""
    adapter = _BalanceAdapter(730)
    engine = _engine(adapter)
    engine.config.poll_interval = 0
    calls: list[str] = []

    monkeypatch.setattr(engine, "validate_risk_controls", lambda: {})

    async def fake_connect() -> None:
        calls.append("connect")

    async def fake_run_once(*_args, **_kwargs) -> str:
        calls.append("run_once")
        engine.stop()
        return "完成"

    monkeypatch.setattr(engine, "connect", fake_connect)
    monkeypatch.setattr(engine, "run_once", fake_run_once)

    with caplog.at_level(logging.INFO, logger="grid_engine"):
        asyncio.run(engine.run_forever())

    assert calls == ["connect", "run_once"]
    assert adapter.balance_calls == 1
    assert "库存上限状态" in caplog.text
    assert "当前生效值=$2190.00" in caplog.text
    assert "上限来源=权益比例" in caplog.text


def test_startup_cache_is_still_processed_by_first_drawdown_check(
    monkeypatch,
    tmp_path,
) -> None:
    """启动摘要预热缓存后，首轮熔断仍须播种同一份权益且不得重复查询。"""
    adapter = _BalanceAdapter(1000)
    engine = _engine(adapter)
    engine.config.max_drawdown_pct = 0.12
    engine.config.poll_interval = 0
    engine.config.state_path = str(tmp_path / "grid_state.json")

    monkeypatch.setattr(engine, "validate_risk_controls", lambda: {})

    async def fake_connect() -> None:
        return None

    monkeypatch.setattr(engine, "connect", fake_connect)

    async def first_round() -> str:
        assert await engine._check_equity_drawdown() is False
        engine.stop()
        return "完成"

    monkeypatch.setattr(engine, "run_once", first_round)

    asyncio.run(engine.run_forever())

    assert adapter.balance_calls == 1
    assert engine._equity_peak == 1000.0


def test_unconfigured_ratio_does_not_prefetch_balance_at_startup(
    monkeypatch,
) -> None:
    """无比例的 legacy 启动不得比变更前多发一次权益请求。"""
    adapter = _BalanceAdapter(RuntimeError("不应调用"))
    engine = _engine(adapter, ratio=None)
    engine.config.max_drawdown_pct = 0.12
    engine.config.poll_interval = 0

    monkeypatch.setattr(engine, "validate_risk_controls", lambda: {})

    async def fake_connect() -> None:
        return None

    async def first_round() -> str:
        engine.stop()
        return "完成"

    monkeypatch.setattr(engine, "connect", fake_connect)
    monkeypatch.setattr(engine, "run_once", first_round)

    asyncio.run(engine.run_forever())

    assert adapter.balance_calls == 0


def test_extended_cli_wires_optional_wallet_exposure_ratio() -> None:
    """Extended 入口默认不启用比例，显式传入时透传到引擎配置。"""
    defaults = run_grid._build_parser().parse_args([])
    enabled = run_grid._build_parser().parse_args(
        ["--wallet-exposure-ratio", "3"]
    )

    assert defaults.wallet_exposure_ratio is None
    assert run_grid._grid_config(defaults).wallet_exposure_ratio is None
    assert enabled.wallet_exposure_ratio == 3.0
    assert run_grid._grid_config(enabled).wallet_exposure_ratio == 3.0


def test_lighter_cli_wires_optional_wallet_exposure_ratio() -> None:
    """Lighter 入口默认不启用比例，显式传入时透传到引擎配置。"""
    defaults = run_lighter_mm.build_parser().parse_args([])
    enabled = run_lighter_mm.build_parser().parse_args(
        ["--wallet-exposure-ratio", "3"]
    )

    assert defaults.wallet_exposure_ratio is None
    assert run_lighter_mm._grid_config(defaults).wallet_exposure_ratio is None
    assert enabled.wallet_exposure_ratio == 3.0
    assert run_lighter_mm._grid_config(enabled).wallet_exposure_ratio == 3.0


def test_existing_cli_arguments_keep_absolute_caps_without_ratio() -> None:
    """两个入口不传新参数时保留原有绝对上限实参。"""
    extended = run_grid._grid_config(
        run_grid._build_parser().parse_args(["--max-inv", "3750"])
    )
    lighter = run_lighter_mm._grid_config(
        run_lighter_mm.build_parser().parse_args(["--max-inv", "3750"])
    )

    assert (extended.max_inventory_usd, extended.wallet_exposure_ratio) == (
        3750.0,
        None,
    )
    assert (lighter.max_inventory_usd, lighter.wallet_exposure_ratio) == (
        3750.0,
        None,
    )
