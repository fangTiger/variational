"""挂单去重、翻单重试与重试前对账测试。"""
from __future__ import annotations

import asyncio
from decimal import ROUND_DOWN, Decimal
from types import SimpleNamespace

from adapters.base import Side
from grid.grid_engine import GridConfig, GridEngine


class StubExt:
    """桩：挂单成功后订单会出现在盘口（避免假的“从盘口消失”）。"""

    def __init__(self, fail_times=0, preexisting=None, history=None):
        self.fail_times = fail_times
        self.open_orders = list(preexisting or [])
        self.history = list(history or [])
        self.placed: list[dict] = []
        self.open_reads = 0
        self.history_reads = 0

    async def get_open_orders(self, market):
        self.open_reads += 1
        return self.open_orders

    async def get_orders_history(self, market, limit=100, **kwargs):
        self.history_reads += 1
        return self.history

    async def round_price(self, market, price):
        """按 $1 tick 对齐，覆盖价格精度匹配。"""
        return Decimal(str(int(Decimal(str(price)))))

    async def round_amount(self, market, amount):
        """按 0.00001 步长向下对齐，覆盖数量精度匹配。"""
        amount = Decimal(str(amount))
        step = Decimal("0.00001")
        return (amount / step).to_integral_value(rounding=ROUND_DOWN) * step

    async def place_limit_order(self, market, side, amount, price, **kw):
        if self.fail_times > 0:
            self.fail_times -= 1
            raise RuntimeError("模拟网络故障")
        oid = f"ok-{len(self.placed) + 1}"
        aligned_amount = await self.round_amount(market, amount)
        self.placed.append({"side": side, "price": float(price)})
        self.open_orders.append(
            SimpleNamespace(id=oid, side=side.value, price=price, qty=aligned_amount)
        )
        return SimpleNamespace(data=SimpleNamespace(id=oid))


def _eng(ext):
    # 2% 档距让格 558 约为 $63k，$20 数量按步长对齐后为 0.00031。
    return GridEngine(ext, GridConfig(dry_run=False, unit_usd=20.0, spacing_pct=0.02))


def test_duplicate_level_is_skipped() -> None:
    """同档已有跟踪单 → 不重复挂、不覆盖记录。"""
    ext = StubExt()
    eng = _eng(ext)
    eng._orders = {558: {"id": "existing", "side": Side.BUY}}
    asyncio.run(eng._place(558, Side.BUY, 0.0, why="测试"))
    assert not ext.placed
    assert eng._orders[558]["id"] == "existing"
    assert eng._counters["dup_skipped"] == 1


def test_level_in_retry_queue_is_also_occupied() -> None:
    """在重试队列里的档位同样算已占用，普通补格不得绕过。"""
    ext = StubExt()
    eng = _eng(ext)
    eng._retry = {558: {"side": Side.BUY, "qty": None, "why": "翻单", "attempts": 1}}
    asyncio.run(eng._place(558, Side.BUY, 0.0, why="补买格"))
    assert not ext.placed, "队列中的档位不得被普通补格抢先挂出"


def test_retrying_flag_bypasses_retry_dedup_only() -> None:
    """红队 P0-3：drain 必须能绕过 _retry 去重，否则永远重试不了。"""
    ext = StubExt()
    eng = _eng(ext)
    eng._retry = {558: {"side": Side.BUY, "qty": None, "why": "翻单", "attempts": 1}}

    asyncio.run(eng._place(558, Side.BUY, 0.0, why="翻单", retrying=True))
    assert len(ext.placed) == 1, "drain 路径必须能真正挂出"

    eng._orders = {559: {"id": "existing", "side": Side.BUY}}
    asyncio.run(eng._place(559, Side.BUY, 0.0, why="翻单", retrying=True))
    assert len(ext.placed) == 1, "retrying 不得绕过 _orders 去重"


def test_failed_placement_is_retried_next_round() -> None:
    """挂单失败进队列，下一轮 drain 时重试成功并出队。"""
    ext = StubExt(fail_times=1)
    eng = _eng(ext)
    asyncio.run(eng._place(558, Side.BUY, 0.0, why="翻单"))
    assert not ext.placed and 558 in eng._retry
    asyncio.run(eng._handle_fills(0.0, blocked_side=None))
    assert len(ext.placed) == 1
    assert 558 not in eng._retry


def test_retry_reconciles_before_replacing() -> None:
    """交易所其实已收单 → 重试时必须接管而不是重挂。"""
    eng = _eng(StubExt())
    price = eng._level_price(558)
    existing = SimpleNamespace(id="already-there", side="BUY", price=price, qty="0.00031")
    ext = StubExt(preexisting=[existing])
    eng = _eng(ext)
    eng._retry = {558: {"side": Side.BUY, "qty": None, "why": "翻单", "attempts": 1}}

    asyncio.run(eng._handle_fills(0.0, blocked_side=None))

    assert not ext.placed, "已存在匹配单时不得重复挂"
    assert eng._orders[558]["id"] == "already-there", "必须接管该单"
    assert 558 not in eng._retry


def test_retry_reconciles_via_history_when_already_filled() -> None:
    """红队 P0-4：超时但原单已成交时，必须靠 history 兜住。"""
    eng = _eng(StubExt())
    price = eng._level_price(558)
    filled = SimpleNamespace(
        id="timed-out-but-filled",
        side="BUY",
        price=price,
        status="FILLED",
        filled_qty="0.00031",
        order_type="LIMIT",
    )
    ext = StubExt(history=[filled])
    eng = _eng(ext)
    eng._retry = {558: {"side": Side.BUY, "qty": None, "why": "翻单", "attempts": 1}}

    asyncio.run(eng._handle_fills(0.0, blocked_side=None))

    assert not ext.placed, "原单已成交时绝不能重挂（会造成重复实盘单）"
    assert 558 not in eng._retry, "已闭环的档位必须出队"


def test_multiple_matches_do_not_auto_resolve() -> None:
    """对账命中多笔 → 只告警交人工，不得自作主张接管或重挂。"""
    eng = _eng(StubExt())
    price = eng._level_price(558)
    dup = [
        SimpleNamespace(id="a", side="BUY", price=price, qty="0.00031"),
        SimpleNamespace(id="b", side="BUY", price=price, qty="0.00031"),
    ]
    ext = StubExt(preexisting=dup)
    eng = _eng(ext)
    eng._retry = {558: {"side": Side.BUY, "qty": None, "why": "翻单", "attempts": 1}}

    asyncio.run(eng._handle_fills(0.0, blocked_side=None))

    assert not ext.placed, "歧义状态下不得重挂"
    assert 558 in eng._retry, "歧义状态保留在队列里等人工处理"
    assert (ext.open_reads, ext.history_reads) == (2, 1), (
        "drain 双源快照只能各取一次；随后成交检测会再读一次盘口"
    )


def test_drain_only_when_fully_active() -> None:
    """FROZEN 只传单侧 blocked_side，另一侧也不得 drain。"""
    ext = StubExt(fail_times=1)
    eng = _eng(ext)
    asyncio.run(eng._place(558, Side.BUY, 0.0, why="翻单"))
    asyncio.run(eng._handle_fills(0.0, blocked_side="SELL"))
    assert not ext.placed, "非 ACTIVE 状态一律不 drain"
    assert 558 in eng._retry


def test_attempts_accumulate_and_exhaust_without_dropping() -> None:
    """重试次数必须累加；达上限只暂停不删除意图。"""
    ext = StubExt(fail_times=99)
    eng = _eng(ext)
    asyncio.run(eng._place(558, Side.BUY, 0.0, why="翻单"))
    for _ in range(12):
        # 每轮写请求预算由 run_once 在轮次开头重置；本测试直接调用
        # _handle_fills 绕过了 run_once，必须自己模拟这一步，
        # 否则 12 轮共用一份预算，第一轮耗尽后余下轮次全被限流。
        eng._write_budget = eng.config.max_writes_per_round
        asyncio.run(eng._handle_fills(0.0, blocked_side=None))
    assert 558 in eng._retry, "达上限后不得删除翻单意图"
    assert eng._retry[558]["exhausted"] is True
    assert eng._retry[558]["attempts"] >= 10
