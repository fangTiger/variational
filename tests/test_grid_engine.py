"""网格引擎翻单逻辑测试：按订单终态区分 成交/过期/被拒。

背景（2026-07-20 实盘事故）：原实现把"挂单从盘口消失"一律当成交，
导致过期(EXPIRED)单被错误翻单、翻单价穿过盘口(靠 post_only 被拒才没亏钱)。
本测试锁定修复后的行为：只有 FILLED(或有部分成交量)才翻单。
"""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

from adapters.base import Side
from grid.grid_engine import GridConfig, GridEngine


class FakeExt:
    """最小桩：只实现 _handle_fills 用到的接口。"""

    def __init__(self, open_orders=None, history=None):
        self.open_orders = open_orders or []
        self.history = history or []
        self.placed: list[dict] = []

    async def get_open_orders(self, market):
        return self.open_orders

    async def get_orders_history(self, market, limit=100, **kwargs):
        return self.history

    async def place_limit_order(self, market, side, amount, price, **kw):
        self.placed.append({"side": side, "amount": float(amount), "price": float(price)})
        return SimpleNamespace(data=SimpleNamespace(id=f"new-{len(self.placed)}", status="NEW"))


def _engine(ext, orders):
    eng = GridEngine(ext, GridConfig(dry_run=False))
    eng._orders = dict(orders)
    return eng


def _hist(oid, status, filled_qty=0):
    return SimpleNamespace(id=oid, status=status, filled_qty=filled_qty)


def test_filled_order_flips(caplog) -> None:
    """真成交 → 翻反向一格。"""
    ext = FakeExt(open_orders=[], history=[_hist("o1", "FILLED")])
    eng = _engine(ext, {558: {"id": "o1", "side": Side.BUY}})
    with caplog.at_level(logging.INFO, logger="grid_engine"):
        asyncio.run(eng._handle_fills(0.0))
    assert 558 not in eng._orders
    assert len(ext.placed) == 1 and ext.placed[0]["side"] is Side.SELL
    assert 559 in eng._orders  # 买成交 → 上一格挂卖
    assert any(
        "格558" in record.getMessage() and "成交" in record.getMessage()
        for record in caplog.records
    )


def test_expired_order_replaced_same_level(caplog) -> None:
    """过期 → 原格原方向重挂，不翻单。"""
    ext = FakeExt(open_orders=[], history=[_hist("o1", "EXPIRED")])
    eng = _engine(ext, {560: {"id": "o1", "side": Side.SELL}})
    with caplog.at_level(logging.INFO, logger="grid_engine"):
        asyncio.run(eng._handle_fills(0.0))
    assert len(ext.placed) == 1 and ext.placed[0]["side"] is Side.SELL
    assert eng._orders[560]["id"] == "new-1"  # 同格新单
    assert any(
        "EXPIRED" in record.getMessage() and "重挂" in record.getMessage()
        for record in caplog.records
    )


def test_partial_fill_then_expired_flips() -> None:
    """带部分成交量的过期单按成交处理（翻单），不丢库存。"""
    ext = FakeExt(open_orders=[], history=[_hist("o1", "EXPIRED", filled_qty=0.0004)])
    eng = _engine(ext, {558: {"id": "o1", "side": Side.BUY}})
    asyncio.run(eng._handle_fills(0.0))
    assert len(ext.placed) == 1 and ext.placed[0]["side"] is Side.SELL


def test_frozen_side_not_replenished(caplog) -> None:
    """冻结 BUY 时，买单过期不重挂买单。"""
    ext = FakeExt(open_orders=[], history=[_hist("o1", "EXPIRED")])
    eng = _engine(ext, {558: {"id": "o1", "side": Side.BUY}})
    with caplog.at_level(logging.INFO, logger="grid_engine"):
        asyncio.run(eng._handle_fills(0.0, blocked_side="BUY"))
    assert ext.placed == []
    assert any(
        "格558" in record.getMessage() and "冻结" in record.getMessage()
        for record in caplog.records
    )


def test_partial_fill_uses_filled_qty() -> None:
    """部分成交翻单数量按 filled_qty，不按整格。"""
    ext = FakeExt(open_orders=[], history=[_hist("o1", "FILLED", filled_qty=0.0002)])
    eng = _engine(ext, {558: {"id": "o1", "side": Side.BUY}})
    asyncio.run(eng._handle_fills(0.0))
    assert len(ext.placed) == 1
    assert ext.placed[0]["amount"] == 0.0002


def test_rejected_order_dropped_without_flip() -> None:
    """被拒(POST_ONLY_FAILED 等) → 仅移除记录，不翻单不重挂。"""
    ext = FakeExt(open_orders=[], history=[_hist("o1", "REJECTED")])
    eng = _engine(ext, {560: {"id": "o1", "side": Side.BUY}})
    asyncio.run(eng._handle_fills(0.0))
    assert 560 not in eng._orders
    assert ext.placed == []


def test_unknown_status_retained_for_retry_without_flip() -> None:
    """历史查不到终态 → 保留跟踪记录下轮重试，但绝不翻单。"""
    ext = FakeExt(open_orders=[], history=[])
    eng = _engine(ext, {560: {"id": "o1", "side": Side.SELL}})
    asyncio.run(eng._handle_fills(0.0))
    assert 560 in eng._orders
    assert ext.placed == []


def test_still_open_untouched(caplog) -> None:
    """仍在盘口的单不动。"""
    ext = FakeExt(open_orders=[SimpleNamespace(id="o1")], history=[])
    eng = _engine(ext, {560: {"id": "o1", "side": Side.SELL}})
    with caplog.at_level(logging.DEBUG, logger="grid_engine"):
        asyncio.run(eng._handle_fills(0.0))
    assert eng._orders[560]["id"] == "o1"
    assert ext.placed == []
    assert any("仍在盘口" in record.getMessage() for record in caplog.records)


def test_cancel_keeps_record_on_failure() -> None:
    """撤单失败时不能把订单记录丢掉（否则留孤儿单）。"""

    class FailExt(FakeExt):
        async def cancel_order(self, market, order_id):
            raise RuntimeError("network")

    ext = FailExt()
    eng = _engine(ext, {560: {"id": "o1", "side": Side.SELL}})
    asyncio.run(eng._cancel(560, why="test"))
    assert 560 in eng._orders  # 撤单失败 → 记录保留，下轮重试


def test_within_cap_uses_real_inventory(caplog) -> None:
    """已有多头库存接近上限时，不应再允许新增买单。"""
    ext = FakeExt()
    eng = GridEngine(ext, GridConfig(dry_run=False, unit_usd=200, max_inventory_usd=1600))
    # 已持多头 $1500，再挂 1 张 $200 买单会到 $1700 > 上限
    assert eng._within_cap(Side.BUY, inv_usd=1500.0) is False
    # 持多头 $1000，挂 $200 到 $1200 < 上限 → 允许
    assert eng._within_cap(Side.BUY, inv_usd=1000.0) is True

    with caplog.at_level(logging.INFO, logger="grid_engine"):
        asyncio.run(eng._place(558, Side.BUY, inv_usd=1500.0, why="补买格"))
        asyncio.run(eng._place(559, Side.BUY, inv_usd=1500.0, why="补买格"))
    cap_messages = [
        record.getMessage()
        for record in caplog.records
        if "库存上限拒绝挂单" in record.getMessage()
    ]
    assert len(cap_messages) == 1, "相同库存拒绝不得每轮重复打 INFO"
    assert all(
        item in cap_messages[0]
        for item in (
            "格558",
            "方向=BUY",
            "inventory_usd=",
            "pending_usd=",
            "unit=",
            "max_inventory_usd=",
        )
    )


def test_within_cap_same_orders_respects_real_inventory() -> None:
    """同样的挂单数量下，是否可加仓必须由真实持仓共同决定。"""
    ext = FakeExt()
    eng = GridEngine(ext, GridConfig(dry_run=False, unit_usd=200, max_inventory_usd=1600))
    eng._orders = {
        558: {"id": "b1", "side": Side.BUY},
        559: {"id": "b2", "side": Side.BUY},
    }
    # 两张已挂买单 $400 + 一张新买单 $200：真实多头 $800 时最坏为 $1400。
    assert eng._within_cap(Side.BUY, inv_usd=800.0) is True
    # 挂单数不变，真实多头升至 $1100 时最坏为 $1700，应拒绝。
    assert eng._within_cap(Side.BUY, inv_usd=1100.0) is False


if __name__ == "__main__":
    test_filled_order_flips()
    test_expired_order_replaced_same_level()
    test_partial_fill_then_expired_flips()
    test_rejected_order_dropped_without_flip()
    test_unknown_status_retained_for_retry_without_flip()
    test_still_open_untouched()
    print("✅ grid_engine 测试通过")
