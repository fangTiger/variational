"""TPSL 触发价必须按市场 tick 对齐后再比较，否则幂等永远失效。

2026-07-30 实盘事故：引擎算出 73900.40316548387...，交易所按 tick=1 取整存为 73901，
差值 0.597；而容差是 max(0.1, 73900*0.000001)=0.1，小于半个 tick。
结果每轮都判"漂移"重挂，连续六轮触发价完全相同，两个 P0 修复同时被打回原形。

正解：引擎自己先把触发价取整到 tick 再挂、再缓存、再比较，而不是拍一个固定容差。
"""
from __future__ import annotations

import asyncio
import logging
from decimal import Decimal, ROUND_HALF_UP
from types import SimpleNamespace

from tests.test_trend_aware_engine import StopExt, _eng


class TickExt(StopExt):
    """带 tick 对齐能力的桩：round_price 模拟交易所 tick=1 的取整。"""

    TICK = Decimal("1")

    def __init__(self, positions, liq=None):
        super().__init__(positions=positions, liq=liq)
        self.tpsl_on_book = None

    async def round_price(self, market, price):
        return Decimal(str(price)).quantize(self.TICK, rounding=ROUND_HALF_UP)

    async def get_position_tpsl(self, market):
        return self.tpsl_on_book

    async def place_position_stop_loss(self, market, signed_size, trigger_price):
        await super().place_position_stop_loss(market, signed_size, trigger_price)
        # 交易所永远按 tick 取整后存储——这是事故的关键
        stored = Decimal(str(trigger_price)).quantize(self.TICK, rounding=ROUND_HALF_UP)
        self.tpsl_on_book = SimpleNamespace(
            id="tpsl-1",
            status="UNTRIGGERED",
            stop_loss=SimpleNamespace(trigger_price=stored),
        )


def _short(tmp_path):
    ext = TickExt(positions=[Decimal("-0.003")], liq=None)
    return ext, _eng(ext, tmp_path / "s.json")


def test_tpsl_idempotent_when_exchange_rounds_to_tick(tmp_path) -> None:
    """交易所按 tick 取整导致的亚 tick 差异，绝不能被误判为漂移。

    这是 2026-07-30 实盘事故的直接回归测试。
    """
    ext, eng = _short(tmp_path)

    asyncio.run(eng._maintain_tpsl(mark=64360.0, signed_size=Decimal("-0.003")))
    asyncio.run(eng._maintain_tpsl(mark=64360.0, signed_size=Decimal("-0.003")))
    asyncio.run(eng._maintain_tpsl(mark=64360.0, signed_size=Decimal("-0.003")))

    assert len(ext.position_stop_losses) == 1, (
        f"亚 tick 差异被误判为漂移，重挂了 {len(ext.position_stop_losses)} 次"
    )


def test_tpsl_trigger_is_tick_aligned_when_placed(tmp_path) -> None:
    """挂出的触发价本身就应该是 tick 对齐的，不该把无限小数丢给交易所。"""
    ext, eng = _short(tmp_path)

    asyncio.run(eng._maintain_tpsl(mark=64360.0, signed_size=Decimal("-0.003")))

    _, _, trigger = ext.position_stop_losses[0]
    trigger = Decimal(str(trigger))
    assert trigger == trigger.quantize(TickExt.TICK), f"触发价未按 tick 对齐: {trigger}"


def test_tpsl_still_replaced_on_real_drift(tmp_path) -> None:
    """真正超过一个 tick 的漂移仍必须重挂——修容差不能把保护一起修没。"""
    ext, eng = _short(tmp_path)
    asyncio.run(eng._maintain_tpsl(mark=64360.0, signed_size=Decimal("-0.003")))

    ext.tpsl_on_book = SimpleNamespace(
        id="drifted",
        status="UNTRIGGERED",
        stop_loss=SimpleNamespace(trigger_price=Decimal("73950")),
    )

    asyncio.run(eng._maintain_tpsl(mark=64360.0, signed_size=Decimal("-0.003")))

    assert len(ext.position_stop_losses) == 2, "真实漂移必须重挂"


# ---------- 静默路径必须可观测 ----------

def test_place_logs_reason_when_blocked_by_inventory_cap(tmp_path, caplog) -> None:
    """库存上限拒绝挂单时必须留下日志，不能静默 return None。

    2026-07-30 事故中卖单从 8 张掉到 6 张且零日志输出，只能靠手算 _within_cap
    反推原因。没有可观测性，下次出问题还是只能猜。
    """
    ext = TickExt(positions=[Decimal("-0.003")], liq=None)
    eng = _eng(ext, tmp_path / "s.json")
    from adapters.base import Side

    # 造出必然超上限的局面：卖侧已有挂单占满额度
    unit = eng.config.unit_usd
    cap = eng.config.max_inventory_usd
    n = int(cap / unit) + 1
    eng._orders = {i: {"id": f"o{i}", "side": Side.SELL} for i in range(n)}

    with caplog.at_level(logging.INFO, logger="grid_engine"):
        result = asyncio.run(
            eng._place(999, Side.SELL, inv_usd=-200.0, why="补卖格")
        )

    assert result is None, "超上限时确实应该拒绝挂单"
    assert caplog.records, "被库存上限拒绝时必须打日志，否则无法排查"
    text = " ".join(r.getMessage() for r in caplog.records)
    assert "上限" in text or "cap" in text.lower(), f"日志未说明拒绝原因: {text}"
