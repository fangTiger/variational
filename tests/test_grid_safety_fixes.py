"""三方评审（Claude + 2×Codex）确认的四项缺陷的回归测试。

对应 2026-07-30 评审结论：
- 急停链在撤单失败后仍撤 TPSL，可留下无保护裸仓
- halted 状态下不再维护 TPSL，裸仓永久无保护
- 订单终态查不到即丢弃跟踪记录，导致翻单丢失
- 整仓 TPSL 每轮无条件重挂，污染订单历史窗口且双向漂移

这些测试全部先失败（RED），实现后转绿。
"""
from __future__ import annotations

import asyncio
from decimal import Decimal
from types import SimpleNamespace

from adapters.base import Position, Side
from grid.grid_engine import GridConfig, GridEngine
from grid.grid_state import GridState, save_state

from tests.test_trend_aware_engine import RunExt, StopExt, _eng


def _order(oid, reduce_only=False, otype="LIMIT"):
    return SimpleNamespace(id=oid, reduce_only=reduce_only, type=otype)


# ---------- 缺陷 1：急停链撤 TPSL 前未确认残单已撤净 ----------

def test_go_off_keeps_tpsl_when_grid_cancel_failed(tmp_path) -> None:
    """撤网格单抛异常时，即使连续两次读到空仓也绝不能撤 TPSL。

    否则残留 LIMIT 单随后成交会重新开仓，而此时已无任何止损保护。
    """
    ext = StopExt(positions=[0.02, 0.0, 0.0], liq=None)
    ext.open_orders = [_order("residual-limit")]

    async def cancel_boom(market):
        raise RuntimeError("撤网格单接口失败")

    ext.cancel_grid_orders = cancel_boom
    eng = _eng(ext, tmp_path / "s.json")

    asyncio.run(eng._go_off_confirmed(Decimal("0.02")))

    assert ext.tpsl_cancelled == 0, "撤单失败后撤掉 TPSL 会留下无保护裸仓"


def test_go_off_keeps_tpsl_while_residual_grid_orders_on_book(tmp_path) -> None:
    """撤单调用成功但盘口仍有普通 LIMIT 残单时，同样不得撤 TPSL。"""
    ext = StopExt(positions=[0.02, 0.0, 0.0], liq=None)
    ext.open_orders = [_order("still-there")]
    eng = _eng(ext, tmp_path / "s.json")

    asyncio.run(eng._go_off_confirmed(Decimal("0.02")))

    assert ext.tpsl_cancelled == 0, "盘口仍有残单时撤 TPSL 会留下无保护裸仓"


# ---------- 缺陷 2：halted 状态下不再维护 TPSL ----------

def test_halted_round_still_maintains_tpsl_when_position_exists(tmp_path) -> None:
    """halted=true 且仍有仓位时，每轮必须继续维护整仓 TPSL。

    急停后残单成交会产生新仓；当前 halted 分支在 _maintain_tpsl 之前 return，
    该仓位将永远得不到止损保护。
    """
    path = tmp_path / "s.json"
    save_state(
        str(path),
        GridState(
            band_low=90.0,
            band_high=110.0,
            frozen=False,
            blocked_side=None,
            halted=True,
        ),
    )
    ext = RunExt(positions=[Decimal("0.01")], liq=(100.0, 200.0), mark=100.0)
    eng = _eng(ext, path)

    asyncio.run(eng.run_once())

    assert ext.position_stop_losses, "halted 且有仓位时必须维护 TPSL，不能裸奔"


# ---------- 缺陷 3：终态查不到即丢弃跟踪记录 ----------

def _fills_engine(tmp_path, ext):
    eng = GridEngine(
        ext,
        GridConfig(
            dry_run=False,
            trend_aware=True,
            hard_stop_dist=0.12,
            state_path=str(tmp_path / "s.json"),
        ),
    )
    return eng


def test_unresolved_order_stays_tracked_for_retry(tmp_path) -> None:
    """订单从盘口消失但历史里查不到终态时，必须保留 _orders 记录下轮重试。

    当前实现直接 pop 掉并放弃翻单，成交利润永久丢失。
    """
    ext = StopExt(positions=[0.0], liq=None)
    ext.open_orders = []          # 跟踪单已从盘口消失
    eng = _fills_engine(tmp_path, ext)
    eng._orders = {2218: {"id": "vanished", "side": Side.BUY}}

    async def empty_history(market, limit=100):
        return []                  # 历史窗口里查不到（被 TPSL 洪水挤出）

    ext.get_orders_history = empty_history

    asyncio.run(eng._handle_fills(inv_usd=0.0))

    assert 2218 in eng._orders, "终态未解析前不得丢弃跟踪记录，否则翻单丢失"


def test_order_missing_from_history_is_resolved_by_id_lookup(tmp_path) -> None:
    """历史分页里没有该订单时，必须按 ID 单查兜底，查到 FILLED 就要翻单。"""
    ext = StopExt(positions=[0.0], liq=None)
    ext.open_orders = []
    eng = _fills_engine(tmp_path, ext)
    eng._orders = {2218: {"id": "old-but-filled", "side": Side.BUY}}

    async def empty_history(market, limit=100):
        return []

    async def by_id(market, order_id):
        assert order_id == "old-but-filled"
        return SimpleNamespace(
            id=order_id, status="FILLED", filled_qty=Decimal("0.003")
        )

    ext.get_orders_history = empty_history
    ext.get_order_by_id = by_id

    asyncio.run(eng._handle_fills(inv_usd=0.0))

    assert ext.placed, "按 ID 查到 FILLED 后必须在上一格挂出止盈卖单"
    assert ext.placed[0]["side"] is Side.SELL


def test_history_query_filters_to_limit_orders_by_update_time(tmp_path) -> None:
    """查终态必须在服务端按 LIMIT 类型 + 更新时间排序，不能拉回默认分页再本地过滤。"""
    ext = StopExt(positions=[0.0], liq=None)
    ext.open_orders = []
    eng = _fills_engine(tmp_path, ext)
    eng._orders = {2218: {"id": "x", "side": Side.BUY}}
    seen = {}

    async def history(market, limit=100, **kwargs):
        seen.update(kwargs)
        return []

    ext.get_orders_history = history
    asyncio.run(eng._handle_fills(inv_usd=0.0))

    assert seen.get("order_type") == "LIMIT", "必须服务端过滤掉 TPSL，否则窗口被冲刷"
    assert seen.get("sort") == "UPDATED_AT", "必须按更新时间排序，否则老订单永远在窗口外"


# ---------- 缺陷 4：TPSL 每轮无条件重挂 + 双向漂移 ----------

def test_tpsl_not_replaced_when_position_and_trigger_unchanged(tmp_path) -> None:
    """仓位与触发价都没变时，不得重复挂 TPSL（每天 2700 次调用冲刷订单历史）。"""
    ext = StopExt(positions=[Decimal("-0.003")], liq=None)
    eng = _eng(ext, tmp_path / "s.json")

    asyncio.run(eng._maintain_tpsl(mark=64000.0, signed_size=Decimal("-0.003")))
    asyncio.run(eng._maintain_tpsl(mark=64000.0, signed_size=Decimal("-0.003")))

    assert len(ext.position_stop_losses) == 1, "同仓同价必须幂等，不能每轮重挂"


def test_short_tpsl_trigger_never_loosens_as_price_rises(tmp_path) -> None:
    """空头止损只能保持或收紧，绝不能随 mark 上行而被推高（否则永不触发）。"""
    ext = StopExt(positions=[Decimal("-0.003")], liq=None)
    eng = _eng(ext, tmp_path / "s.json")

    asyncio.run(eng._maintain_tpsl(mark=64000.0, signed_size=Decimal("-0.003")))
    first = ext.position_stop_losses[0][2]

    # 价格朝不利方向走 3%
    asyncio.run(eng._maintain_tpsl(mark=65920.0, signed_size=Decimal("-0.003")))
    latest = ext.position_stop_losses[-1][2]

    assert Decimal(str(latest)) <= Decimal(str(first)), (
        "空头 TPSL 随 mark 上行而放松，等于每轮把保护重置到 12% 之外"
    )
