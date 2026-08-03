"""每轮请求预算测试：任何单轮都不得退化成 80 次串行请求。"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from adapters.base import Side
from grid.grid_engine import GridConfig, GridEngine


class CountingExt:
    def __init__(self) -> None:
        self.write_calls = 0
        self.by_id_calls = 0
        self.open_orders: list = []

    async def get_open_orders(self, market):
        return self.open_orders

    async def get_orders_history(self, market, limit=100, **kwargs):
        return []

    async def get_order_by_id(self, market, order_id):
        self.by_id_calls += 1
        return None

    async def place_limit_order(self, market, side, amount, price, **kwargs):
        self.write_calls += 1
        oid = f"ok-{self.write_calls}"
        self.open_orders.append(
            SimpleNamespace(id=oid, side=side.value, price=price)
        )
        return SimpleNamespace(data=SimpleNamespace(id=oid, status="NEW"))


def _eng(ext: CountingExt) -> GridEngine:
    config = GridConfig(
        dry_run=False,
        unit_usd=20.0,
        spacing_pct=0.001,
        levels_per_side=40,
        max_inventory_usd=800.0,
    )
    # RED 阶段允许旧 GridConfig 加动态属性，避免“缺字段”掩盖行为断言。
    config.max_writes_per_round = 10
    config.max_by_id_lookups_per_round = 10
    return GridEngine(ext, config)


def _reset_round(eng: GridEngine) -> None:
    """模拟 run_once 开头的两个独立预算重置。"""
    eng._write_budget = eng.config.max_writes_per_round
    eng._by_id_lookup_budget = eng.config.max_by_id_lookups_per_round
    eng._write_budget_exhausted_this_round = False


def test_ladder_respects_write_budget() -> None:
    """空盘铺 80 档时，单轮写请求不得超过预算。"""
    ext = CountingExt()
    eng = _eng(ext)
    _reset_round(eng)

    asyncio.run(eng._maintain_ladder(100.0, 0.0, blocked_side=None))

    assert 0 < ext.write_calls <= eng.config.max_writes_per_round, (
        f"单轮写请求 {ext.write_calls} 超预算，会造成整轮阻塞"
    )


def test_ladder_alternates_sides() -> None:
    """预算中断前必须交替铺 BUY/SELL，不能留下单边盘口。"""
    ext = CountingExt()
    eng = _eng(ext)
    _reset_round(eng)

    asyncio.run(eng._maintain_ladder(100.0, 0.0, blocked_side=None))

    sides = [order.side for order in ext.open_orders]
    assert sides == [Side.BUY.value, Side.SELL.value] * 5, (
        f"单轮没有逐档交替铺双边：{sides}"
    )


def test_ladder_resumes_next_round() -> None:
    """剩余档位必须在后续轮次继续铺满，不能永远铺不完。"""
    ext = CountingExt()
    eng = _eng(ext)
    cumulative_calls = []

    for _ in range(8):
        _reset_round(eng)
        asyncio.run(eng._maintain_ladder(100.0, 0.0, blocked_side=None))
        cumulative_calls.append(ext.write_calls)

    assert cumulative_calls == list(range(10, 81, 10)), (
        f"各轮没有继续补齐剩余档位：{cumulative_calls}"
    )
    assert len(eng._orders) == 80, "8 轮后仍未铺满 80 档"


def test_by_id_lookup_is_capped() -> None:
    """80 张同时终态时，按 ID 兜底有上限且未查完的保持待确认。"""
    ext = CountingExt()
    eng = _eng(ext)
    eng._orders = {
        level: {"id": f"o{level}", "side": Side.BUY}
        for level in range(500, 580)
    }
    _reset_round(eng)

    asyncio.run(eng._handle_fills(0.0, blocked_side=None))

    assert ext.by_id_calls <= eng.config.max_by_id_lookups_per_round, (
        f"按 ID 查询 {ext.by_id_calls} 次，会卡死整轮"
    )
    pending = [record for record in eng._orders.values() if record.get("status_pending")]
    assert pending, "未查完的必须保留 status_pending 到下轮，不得猜测终态"
