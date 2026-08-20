"""网格最小价差保护的失败路径与 8/19 事故回放测试。"""

from __future__ import annotations

import asyncio
import logging
from decimal import Decimal
from types import SimpleNamespace

import pytest

from adapters.base import MarketPrice, Side
from grid.grid_engine import GridConfig, GridEngine


class GuardExt:
    """只走内存的交易所桩，可编排盘口、终态并记录真实下单意图。"""

    def __init__(self, market_result=None) -> None:
        self.market_result = market_result
        self.market_reads = 0
        self.open_orders = []
        self.history = []
        self.placed: list[dict] = []

    async def get_market_price(self, market: str):
        self.market_reads += 1
        if isinstance(self.market_result, BaseException):
            raise self.market_result
        return self.market_result

    async def get_open_orders(self, market: str):
        return self.open_orders

    async def get_orders_history(self, market: str, limit: int = 100, **kwargs):
        return self.history

    async def place_limit_order(self, market, side, amount, price, **kwargs):
        record = {
            "market": market,
            "side": side,
            "amount": Decimal(str(amount)),
            "price": Decimal(str(price)),
            **kwargs,
        }
        self.placed.append(record)
        return SimpleNamespace(
            data=SimpleNamespace(id=f"new-{len(self.placed)}", status="NEW")
        )


def _engine(ext: GuardExt, **config_overrides) -> GridEngine:
    config = GridConfig(
        market="BTC",
        dry_run=False,
        trend_aware=True,
        unit_usd=10.0,
        max_inventory_usd=10000.0,
        **config_overrides,
    )
    return GridEngine(ext, config)


def _run_place_round(monkeypatch, engine: GridEngine, placements) -> str:
    """让 run_once 的业务主体只执行给定挂单，隔离快照缓存行为。"""

    async def fake_trend_round(include_slow: bool = True) -> str:
        del include_slow
        for level, side in placements:
            await engine._place(level, side, 0.0, why="测试挂单")
        return "测试轮完成"

    monkeypatch.setattr(engine, "_run_once_trend_aware", fake_trend_round)
    return asyncio.run(engine.run_once(include_slow=False))


@pytest.mark.parametrize(
    "market_result",
    [RuntimeError("盘口失败"), asyncio.TimeoutError(), None],
    ids=["error", "timeout", "empty"],
)
def test_unavailable_market_price_fails_open_once_per_round(
    monkeypatch,
    caplog,
    market_result,
) -> None:
    """盘口异常、超时或空结果均须整轮放行，且只告警一次。"""
    ext = GuardExt(market_result)
    eng = _engine(ext)
    monkeypatch.setattr(
        eng,
        "_level_price",
        lambda level: {1: Decimal("90"), 2: Decimal("110")}[level],
    )

    with caplog.at_level(logging.WARNING, logger="grid_engine"):
        _run_place_round(
            monkeypatch,
            eng,
            [(1, Side.SELL), (2, Side.BUY)],
        )

    assert len(ext.placed) == 2
    assert ext.market_reads == 1
    assert sum("盘口" in record.getMessage() for record in caplog.records) == 1


@pytest.mark.parametrize("sell_price", [Decimal("100"), Decimal("101")])
def test_sell_at_or_below_best_ask_is_skipped_without_retry(
    monkeypatch,
    caplog,
    sell_price: Decimal,
) -> None:
    """SELL 目标价不高于 ask 时不得发单，也不得制造重试意图。"""
    ext = GuardExt(MarketPrice("BTC", Decimal("100"), Decimal("101")))
    eng = _engine(ext)
    monkeypatch.setattr(eng, "_level_price", lambda _level: sell_price)

    with caplog.at_level(logging.INFO, logger="grid_engine"):
        _run_place_round(monkeypatch, eng, [(1, Side.SELL)])

    assert ext.placed == []
    assert eng._retry == {}
    assert any("SELL" in record.getMessage() and "跳过" in record.getMessage()
               for record in caplog.records)


@pytest.mark.parametrize("buy_price", [Decimal("100"), Decimal("101")])
def test_buy_at_or_above_best_bid_is_skipped_without_retry(
    monkeypatch,
    caplog,
    buy_price: Decimal,
) -> None:
    """BUY 目标价不低于 bid 时不得发单，也不得制造重试意图。"""
    ext = GuardExt(MarketPrice("BTC", Decimal("100"), Decimal("101")))
    eng = _engine(ext)
    monkeypatch.setattr(eng, "_level_price", lambda _level: buy_price)

    with caplog.at_level(logging.INFO, logger="grid_engine"):
        _run_place_round(monkeypatch, eng, [(1, Side.BUY)])

    assert ext.placed == []
    assert eng._retry == {}
    assert any("BUY" in record.getMessage() and "跳过" in record.getMessage()
               for record in caplog.records)


def test_consecutive_cancellations_enter_round_backoff(tmp_path) -> None:
    """同格连续普通取消达到阈值后，当前轮必须停止原格重挂。"""
    ext = GuardExt()
    eng = _engine(
        ext,
        state_path=str(tmp_path / "state.json"),
        cancel_backoff_threshold=2,
        cancel_backoff_rounds=2,
    )
    level = 100
    eng._orders = {level: {"id": "old-1", "side": Side.SELL}}
    ext.history = [SimpleNamespace(id="old-1", status="CANCELLED", filled_qty=0)]

    asyncio.run(eng._handle_fills(0.0))
    assert len(ext.placed) == 1
    assert eng._orders[level]["id"] == "new-1"

    ext.history = [SimpleNamespace(id="new-1", status="CANCELLED", filled_qty=0)]
    asyncio.run(eng._handle_fills(0.0))

    assert len(ext.placed) == 1
    assert level not in eng._orders
    assert level in eng._cancel_backoff
    assert level not in eng._retry


def test_backoff_expires_and_fill_resets_cancellation_state(tmp_path) -> None:
    """退避完整覆盖配置轮数后恢复；退避期间成交会立即清零计数。"""
    ext = GuardExt()
    eng = _engine(
        ext,
        state_path=str(tmp_path / "state.json"),
        cancel_backoff_threshold=1,
        cancel_backoff_rounds=2,
    )
    level = 100
    eng._orders = {level: {"id": "cancel-1", "side": Side.SELL}}
    ext.history = [SimpleNamespace(id="cancel-1", status="CANCELLED", filled_qty=0)]

    asyncio.run(eng._handle_fills(0.0))
    assert ext.placed == []
    ext.history = []
    asyncio.run(eng._handle_fills(0.0))
    assert asyncio.run(eng._place(level, Side.SELL, 0.0, why="退避检查")) is None
    asyncio.run(eng._handle_fills(0.0))
    assert asyncio.run(eng._place(level, Side.SELL, 0.0, why="退避检查")) is None
    asyncio.run(eng._handle_fills(0.0))
    assert asyncio.run(eng._place(level, Side.SELL, 0.0, why="退避期满")) is not None

    fill_level = level + 10
    eng._cancel_counts[fill_level] = 2
    eng._cancel_backoff[fill_level] = {"remaining_rounds": 2}
    eng._orders[fill_level] = {"id": "filled-during-backoff", "side": Side.BUY}
    ext.history = [
        SimpleNamespace(
            id="filled-during-backoff",
            status="FILLED",
            filled_qty=Decimal("0.01"),
        )
    ]

    asyncio.run(eng._handle_fills(0.0))

    assert fill_level not in eng._cancel_counts
    assert fill_level not in eng._cancel_backoff


def test_multiple_orders_in_one_round_read_market_price_once(monkeypatch) -> None:
    """同轮多笔挂单必须复用唯一盘口快照。"""
    ext = GuardExt(MarketPrice("BTC", Decimal("100"), Decimal("101")))
    eng = _engine(ext)
    prices = {1: Decimal("99"), 2: Decimal("102"), 3: Decimal("98")}
    monkeypatch.setattr(eng, "_level_price", lambda level: prices[level])

    _run_place_round(
        monkeypatch,
        eng,
        [(1, Side.BUY), (2, Side.SELL), (3, Side.BUY)],
    )

    assert ext.market_reads == 1
    assert len(ext.placed) == 3


def test_orders_on_correct_side_keep_pre_change_per_order_behavior(monkeypatch) -> None:
    """正确一侧启用保护后，下单参数和本地跟踪结果须与降级路径逐项一致。"""
    quote = MarketPrice("BTC", Decimal("100"), Decimal("101"))
    guarded_ext = GuardExt(quote)
    fallback_ext = GuardExt(RuntimeError("模拟盘口不可用"))
    guarded = _engine(guarded_ext)
    fallback = _engine(fallback_ext)
    prices = {1: Decimal("99"), 2: Decimal("102")}
    monkeypatch.setattr(guarded, "_level_price", lambda level: prices[level])
    monkeypatch.setattr(fallback, "_level_price", lambda level: prices[level])
    placements = [(1, Side.BUY), (2, Side.SELL)]

    _run_place_round(monkeypatch, guarded, placements)
    _run_place_round(monkeypatch, fallback, placements)

    assert guarded_ext.market_reads == 1
    assert guarded_ext.placed == fallback_ext.placed
    assert guarded._orders == fallback._orders


def test_post_only_cancellation_drops_while_ordinary_cancellation_replaces() -> None:
    """post-only 冲突不重挂，普通无成交取消仍保持原格重挂。"""
    ext = GuardExt()
    eng = _engine(ext)
    eng._orders = {
        100: {"id": "post-only", "side": Side.SELL},
        101: {"id": "ordinary", "side": Side.SELL},
    }
    ext.history = [
        SimpleNamespace(
            id="post-only",
            status="CANCELLED-POST-ONLY",
            filled_qty=0,
        ),
        SimpleNamespace(id="ordinary", status="CANCELLED", filled_qty=0),
    ]

    asyncio.run(eng._handle_fills(0.0))

    assert 100 not in eng._orders
    assert 100 not in eng._retry
    assert len(ext.placed) == 1
    assert eng._orders[101]["id"] == "new-1"


def test_august_19_sell_levels_are_skipped_without_retry_loop(monkeypatch) -> None:
    """回放 8/19：ask=64418.7 时两个市价下方卖格跨轮都不得发送。"""
    ext = GuardExt(
        MarketPrice("BTC", Decimal("64418.6"), Decimal("64418.7"))
    )
    eng = _engine(ext)
    prices = {1: Decimal("64233.5"), 2: Decimal("64296.9")}
    monkeypatch.setattr(eng, "_level_price", lambda level: prices[level])

    for _ in range(2):
        _run_place_round(
            monkeypatch,
            eng,
            [(1, Side.SELL), (2, Side.SELL)],
        )

    assert ext.placed == []
    assert eng._orders == {}
    assert eng._retry == {}
    assert ext.market_reads == 2
