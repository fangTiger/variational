"""trend-aware 引擎集成测试：硬止损确认链、空仓/缺失区分、band 状态机。"""
from __future__ import annotations

import asyncio
from decimal import Decimal
from types import SimpleNamespace

from adapters.base import Position, Side
from grid.grid_engine import GridConfig, GridEngine


class StopExt:
    """桩：可编排持仓序列与清算价，记录平仓调用。"""

    def __init__(self, positions, liq=None):
        self._positions = list(positions)  # 依次返回的 signed_size
        self._liq = liq                    # (mark, liq) 或 None
        self.market_orders = []
        self.grid_cancelled = 0
        self.tpsl_cancelled = 0

    async def get_position(self, market):
        size = self._positions[0] if len(self._positions) == 1 else self._positions.pop(0)
        return Position(market=market, signed_size=Decimal(str(size)))

    async def get_liquidation_info(self, market):
        return self._liq

    async def cancel_grid_orders(self, market):
        self.grid_cancelled += 1
        return 0

    async def cancel_tpsl(self, market):
        self.tpsl_cancelled += 1

    async def market_order(self, market, side, amount, *, reduce_only=False):
        self.market_orders.append((side, float(amount), reduce_only))


def _eng(ext):
    return GridEngine(ext, GridConfig(dry_run=False, trend_aware=True, hard_stop_dist=0.12))


def test_flat_position_no_failsafe() -> None:
    """空仓 + 清算价 None → 正常，不触发硬止损。"""
    ext = StopExt(positions=[0.0], liq=None)
    eng = _eng(ext)
    assert asyncio.run(eng._check_hard_stop()) is False


def test_hard_stop_confirms_flat() -> None:
    """距强平 8% < 12% → 平仓；第一次读仍有仓、第二次读为零才算成功。"""
    # 持多头 0.02，mark 100 liq 93 → 距 7% 触发；平仓后仓位序列归零
    ext = StopExt(positions=[0.02, 0.02, 0.0, 0.0], liq=(100.0, 93.0))
    eng = _eng(ext)
    triggered = asyncio.run(eng._check_hard_stop())
    assert triggered is True
    assert ext.grid_cancelled >= 1                 # 撤了网格单
    assert any(ro for (_, _, ro) in ext.market_orders)  # 用了 reduce_only 平仓
    assert ext.tpsl_cancelled >= 1                 # 归零后才撤 TPSL
