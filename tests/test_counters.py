"""计数指标、坏快照告警与进程内经济指标测试。"""
from __future__ import annotations

import asyncio
import json
import logging
from decimal import Decimal
from types import SimpleNamespace

import pytest

from adapters.base import Side
from grid.grid_engine import GridConfig, GridEngine
from grid.regime import GridMode


class CounterExt:
    """只实现成交检测和限价挂单所需接口的交易所桩。"""

    def __init__(self, history=None, open_orders=None, fail_times: int = 0):
        self.history = list(history or [])
        self.open_orders = list(open_orders or [])
        self.fail_times = fail_times
        self.history_calls = 0
        self.placed: list[dict] = []

    async def get_open_orders(self, market):
        return self.open_orders

    async def get_orders_history(self, market, limit=100, **kwargs):
        self.history_calls += 1
        return self.history

    async def place_limit_order(self, market, side, amount, price, **kwargs):
        if self.fail_times > 0:
            self.fail_times -= 1
            raise RuntimeError("模拟挂单失败")
        order_id = f"new-{len(self.placed) + 1}"
        self.placed.append(
            {"id": order_id, "side": side, "amount": amount, "price": price}
        )
        return SimpleNamespace(data=SimpleNamespace(id=order_id))


def _engine(ext: CounterExt, **config_overrides) -> GridEngine:
    config = GridConfig(
        dry_run=False,
        unit_usd=20.0,
        spacing_pct=0.1,
        **config_overrides,
    )
    return GridEngine(ext, config)


def _history_order(
    order_id: str,
    status: str,
    *,
    filled_qty: str = "0",
    average_price: str | None = None,
    payed_fee: str | None = None,
):
    return SimpleNamespace(
        id=order_id,
        status=status,
        filled_qty=filled_qty,
        average_price=average_price,
        payed_fee=payed_fee,
    )


def test_empty_snapshot_warns_but_still_resolves(caplog) -> None:
    """全部跟踪单消失只告警，仍必须查历史并正常处理成交。"""
    history = [
        _history_order(f"o{i}", "FILLED", filled_qty="0.0003")
        for i in range(558, 563)
    ]
    ext = CounterExt(
        history=history,
        open_orders=[SimpleNamespace(id="tpsl-not-tracked")],
    )
    eng = _engine(ext)
    eng._orders = {
        i: {"id": f"o{i}", "side": Side.BUY}
        for i in range(558, 563)
    }

    with caplog.at_level(logging.WARNING, logger="grid_engine"):
        asyncio.run(eng._handle_fills(0.0, blocked_side=None))

    assert eng._counters["bad_snapshot"] == 1, "必须记录疑似坏快照"
    assert ext.history_calls >= 1, "不得因坏快照跳过终态查询"
    assert eng._counters["fill_detected"] == 5, "真实成交必须被识别"
    assert any("疑似坏快照" in record.getMessage() for record in caplog.records)


def test_terminal_unknown_counted_once_per_order() -> None:
    """同一订单持续未知，只计一次。"""
    ext = CounterExt(history=[])
    eng = _engine(ext)
    eng._orders = {558: {"id": "o1", "side": Side.BUY}}

    for _ in range(3):
        asyncio.run(eng._handle_fills(0.0, blocked_side=None))

    assert eng._counters["terminal_unknown"] == 1


def test_ladder_and_relist_do_not_count_as_replacements() -> None:
    """补格和无成交重挂不计翻单；日志文案也不得左右计数口径。"""
    ext = CounterExt()
    eng = _engine(ext)

    asyncio.run(
        eng._place(
            48,
            Side.BUY,
            0.0,
            why="补格日志即使包含成交→翻单字样也不是成交后的翻单",
        )
    )
    ext.history = [_history_order("expired", "EXPIRED")]
    eng._orders[50] = {"id": "expired", "side": Side.SELL}
    asyncio.run(eng._handle_fills(0.0, blocked_side=None))

    assert eng._counters["replacement_placed"] == 0
    assert eng._counters["replacement_failed"] == 0


def test_retry_preserves_explicit_replacement_flag() -> None:
    """重试沿用入队时的显式口径，不得重新解析日志文案。"""
    ext = CounterExt(fail_times=1)
    eng = _engine(ext)

    asyncio.run(
        eng._place(
            48,
            Side.SELL,
            0.0,
            why="不含识别关键词的业务说明",
            is_replacement=True,
        )
    )
    assert eng._retry[48]["is_replacement"] is True
    assert eng._counters["replacement_failed"] == 1

    asyncio.run(eng._handle_fills(0.0, blocked_side=None))

    assert eng._counters["replacement_placed"] == 1
    assert 48 not in eng._retry


def test_opposite_fills_form_one_closed_loop_from_actual_prices() -> None:
    """realized_pnl_net 按实际成交价累计纯价差，不含手续费。"""
    first_fill = _history_order(
        "buy-1",
        "FILLED",
        filled_qty="2",
        average_price="100",
        payed_fee="0.2",
    )
    ext = CounterExt(history=[first_fill])
    eng = _engine(ext)
    eng._orders = {48: {"id": "buy-1", "side": Side.BUY}}

    asyncio.run(eng._handle_fills(0.0, blocked_side=None))
    assert eng._counters["replacement_placed"] == 1

    second_fill = _history_order(
        "new-1",
        "FILLED",
        filled_qty="2",
        average_price="110",
        payed_fee="0.3",
    )
    ext.history = [second_fill]
    asyncio.run(eng._handle_fills(0.0, blocked_side=None))

    assert eng._closed_loops == 1
    assert eng._realized_pnl_net == Decimal("20")


def test_partial_loop_keeps_unmatched_open_leg() -> None:
    """部分反向成交只配对已有数量，剩余腿留待下次配对。"""
    ext = CounterExt(
        history=[
            _history_order(
                "buy-1",
                "FILLED",
                filled_qty="2",
                average_price="100",
                payed_fee="0.2",
            )
        ]
    )
    eng = _engine(ext)
    eng._orders = {48: {"id": "buy-1", "side": Side.BUY}}
    asyncio.run(eng._handle_fills(0.0, blocked_side=None))

    ext.history = [
        _history_order(
            "new-1",
            "EXPIRED",
            filled_qty="1",
            average_price="110",
            payed_fee="0.1",
        )
    ]
    asyncio.run(eng._handle_fills(0.0, blocked_side=None))

    assert eng._closed_loops == 1
    # 配对量 min(1, 2)=1，纯价差 (110-100)×1；原断言 9.8 含两笔按比例分摊的手续费。
    assert eng._realized_pnl_net == Decimal("10")
    assert eng._loop_fills[49]["qty"] == Decimal("1")

    ext.history = [
        _history_order(
            "new-2",
            "FILLED",
            filled_qty="1",
            average_price="102",
            payed_fee="0.12",
        )
    ]
    asyncio.run(eng._handle_fills(0.0, blocked_side=None))

    assert eng._loop_fills[49]["qty"] == Decimal("2")
    assert eng._loop_fills[49]["price"] == Decimal("101")


def test_rejected_terminal_is_counted_from_history() -> None:
    """历史终态 REJECTED 必须计数，不能依赖下单响应的死字段。"""
    ext = CounterExt(history=[_history_order("rejected", "REJECTED")])
    eng = _engine(ext)
    eng._orders = {48: {"id": "rejected", "side": Side.BUY}}

    asyncio.run(eng._handle_fills(0.0, blocked_side=None))

    assert eng._counters["rejected_terminal"] == 1


def test_dump_live_includes_process_counters_and_economics(tmp_path) -> None:
    """live 快照暴露进程内累计值、重试状态和资金费缺口标记。"""
    state_path = tmp_path / "grid_state.json"
    eng = _engine(CounterExt(), state_path=str(state_path))
    eng._counters["fill_detected"] = 7
    eng._retry = {
        48: {"exhausted": False},
        49: {"exhausted": True},
    }
    eng._closed_loops = 2
    eng._realized_pnl_net = Decimal("12.34")

    asyncio.run(
        eng._dump_live(
            mark=100.0,
            mode=GridMode.NEUTRAL,
            inv=Decimal("-3"),
            closes=[],
        )
    )

    payload = json.loads((tmp_path / "grid_live.json").read_text(encoding="utf-8"))
    assert payload["fill_detected"] == 7
    assert payload["retry_pending"] == 1
    assert payload["retry_exhausted"] == 1
    assert payload["counters_started_at"] == eng._counters_started_at
    assert payload["closed_loops"] == 2
    assert payload["realized_pnl_net"] == pytest.approx(12.34)
    assert payload["max_abs_inv_usd"] == pytest.approx(300.0)
    assert payload["funding_included"] is False
