"""trend-aware 引擎集成测试：硬止损确认链、空仓/缺失区分、band 状态机。"""
from __future__ import annotations

import asyncio
import logging
from decimal import Decimal
from types import SimpleNamespace

from adapters.base import Position, Side
from grid.grid_engine import GridConfig, GridEngine
from grid.grid_state import GridState, load_state, save_state
from grid.regime import GridMode


class StopExt:
    """桩：可编排持仓序列与清算价，记录平仓调用。"""

    def __init__(self, positions, liq=None):
        self._positions = list(positions)  # 依次返回的 signed_size
        self._liq = liq                    # (mark, liq) 或 None
        self.position_reads = 0
        self.market_orders = []
        self.grid_cancelled = 0
        self.tpsl_cancelled = 0
        self.position_stop_losses = []
        self.open_orders = []
        self.placed = []

    async def connect(self):
        return None

    async def get_position(self, market):
        self.position_reads += 1
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

    async def place_position_stop_loss(self, market, signed_size, trigger_price):
        self.position_stop_losses.append((market, signed_size, trigger_price))

    async def get_open_orders(self, market):
        return self.open_orders

    async def place_limit_order(self, market, side, amount, price, **kwargs):
        self.placed.append(
            {"side": side, "amount": float(amount), "price": float(price), **kwargs}
        )
        return SimpleNamespace(
            data=SimpleNamespace(id=f"new-{len(self.placed)}", status="NEW")
        )


class RunExt(StopExt):
    """补齐 run_once 所需行情接口，并记录关键调用顺序。"""

    def __init__(self, positions, liq=None, mark=100.0):
        super().__init__(positions=positions, liq=liq)
        self.calls = []
        self._mark = mark
        candles = [
            SimpleNamespace(
                timestamp=str(i),
                high=101.0,
                low=99.0,
                close=100.0,
            )
            for i in range(4)
        ]

        class Info:
            async def get_candles_history(inner_self, **kwargs):
                self.calls.append("candles")
                return SimpleNamespace(data=candles)

            async def get_market_statistics(inner_self, **kwargs):
                self.calls.append("mark")
                return SimpleNamespace(data=SimpleNamespace(mark_price=self._mark))

        self._client = SimpleNamespace(info=Info())

    async def get_position(self, market):
        self.calls.append("position")
        return await super().get_position(market)

    async def get_liquidation_info(self, market):
        self.calls.append("liquidation")
        return await super().get_liquidation_info(market)

    async def get_open_orders(self, market):
        self.calls.append("open_orders")
        return await super().get_open_orders(market)

    async def get_orders_history(self, market, limit=100, **kwargs):
        return []


def _eng(ext, state_path):
    return GridEngine(
        ext,
        GridConfig(
            dry_run=False,
            trend_aware=True,
            hard_stop_dist=0.12,
            state_path=str(state_path),
        ),
    )


def test_flat_position_no_failsafe(tmp_path) -> None:
    """空仓 + 清算价 None → 正常，不触发硬止损。"""
    ext = StopExt(positions=[0.0], liq=None)
    eng = _eng(ext, tmp_path / "s.json")
    assert asyncio.run(eng._check_hard_stop()) is False


def test_hard_stop_confirms_flat(tmp_path) -> None:
    """距强平 8% < 12% → 平仓；第一次读仍有仓、第二次读为零才算成功。"""
    # 持多头 0.02，mark 100 liq 93 → 距 7% 触发；平仓后仓位序列归零
    ext = StopExt(positions=[0.02, 0.02, 0.0, 0.0], liq=(100.0, 93.0))
    eng = _eng(ext, tmp_path / "s.json")
    triggered = asyncio.run(eng._check_hard_stop())
    assert triggered is True
    assert ext.grid_cancelled >= 1                 # 撤了网格单
    assert any(ro for (_, _, ro) in ext.market_orders)  # 用了 reduce_only 平仓
    assert ext.tpsl_cancelled >= 1                 # 归零后才撤 TPSL


def test_hard_stop_uses_position_liquidation_for_distance(tmp_path) -> None:
    """position.raw 有有效强平价时，直接计算距强平距离。"""

    class NoLiquidationRestExt(StopExt):
        async def get_liquidation_info(self, market):
            raise AssertionError("已有 position.raw 时不应再查清算价")

    position = Position(
        market="BTC-USD",
        signed_size=Decimal("0.01"),
        raw=SimpleNamespace(mark_price="100", liquidation_price="80"),
    )
    ext = NoLiquidationRestExt(positions=[0.01])
    eng = _eng(ext, tmp_path / "s.json")

    triggered = asyncio.run(
        eng._check_hard_stop(
            signed_size=position.signed_size,
            mark=100.0,
            position=position,
        )
    )

    assert triggered is False
    assert abs((eng._last_dist_to_liq or 0) - 0.20) < 1e-9


def test_hard_stop_skips_when_position_has_no_liquidation_price(
    tmp_path,
    caplog,
) -> None:
    """position.raw 的 liquidation_price=0 表示不存在强平价，不应进入 ERROR。"""

    class NoLiquidationRestExt(StopExt):
        async def get_liquidation_info(self, market):
            raise AssertionError("liq=0 已可判定为无强平价，不应额外查询")

    position = Position(
        market="BTC-USD",
        signed_size=Decimal("0.00262"),
        raw=SimpleNamespace(mark_price="63500", liquidation_price="0"),
    )
    ext = NoLiquidationRestExt(positions=[position.signed_size])
    eng = _eng(ext, tmp_path / "s.json")

    with caplog.at_level(logging.DEBUG, logger="grid_engine"):
        triggered = asyncio.run(
            eng._check_hard_stop(
                signed_size=position.signed_size,
                mark=63500.0,
                position=position,
            )
        )

    assert triggered is False
    assert eng._last_dist_to_liq is None
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert any("不存在强平价" in r.message for r in caplog.records)


def test_run_once_reuses_position_with_zero_liquidation_price(
    tmp_path,
    caplog,
) -> None:
    """趋势感知主循环复用本轮 position，liq=0 时不额外查清算价。"""

    class ZeroLiquidationRunExt(RunExt):
        async def get_position(self, market):
            self.calls.append("position")
            self.position_reads += 1
            return Position(
                market=market,
                signed_size=Decimal("0.00262"),
                raw=SimpleNamespace(mark_price="63500", liquidation_price="0"),
            )

        async def get_liquidation_info(self, market):
            raise AssertionError("本轮 position 已包含 liq=0，不应额外查询")

    ext = ZeroLiquidationRunExt(positions=[0.00262], liq=None, mark=63500.0)
    eng = GridEngine(
        ext,
        GridConfig(
            dry_run=True,
            trend_aware=True,
            exchange_tpsl=False,
            state_path=str(tmp_path / "s.json"),
        ),
    )

    with caplog.at_level(logging.DEBUG, logger="grid_engine"):
        result = asyncio.run(eng.run_once(include_slow=False))

    assert result == "快速执行完成"
    assert ext.calls.count("position") == 1
    assert "liquidation" not in ext.calls
    assert eng._last_dist_to_liq is None
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


def test_hard_stop_position_unavailable_keeps_failsafe_error(
    tmp_path,
    caplog,
) -> None:
    """完全拿不到仓位数据时，保留 ERROR + fail-safe。"""

    class BrokenPositionExt(StopExt):
        async def get_position(self, market):
            raise RuntimeError("position timeout")

    ext = BrokenPositionExt(positions=[0.01], liq=None)
    eng = _eng(ext, tmp_path / "s.json")

    with caplog.at_level(logging.ERROR, logger="grid_engine"):
        triggered = asyncio.run(eng._check_hard_stop())

    assert triggered is False
    assert eng._last_dist_to_liq is None
    assert any(
        r.levelno >= logging.ERROR
        and "硬止损检查进入 fail-safe" in r.message
        and "仓位数据" in r.message
        for r in caplog.records
    )


def test_dry_run_maintain_tpsl_only_records_intent(tmp_path) -> None:
    """dry_run 维护整仓 TPSL 只视为已确认，绝不能调用交易所下单。"""
    ext = StopExt(positions=[Decimal("0.01")])
    eng = GridEngine(
        ext,
        GridConfig(
            dry_run=True,
            trend_aware=True,
            exchange_tpsl=True,
            state_path=str(tmp_path / "s.json"),
        ),
    )

    result = asyncio.run(
        eng._maintain_tpsl(mark=Decimal("100"), signed_size=Decimal("0.01"))
    )

    assert result is True
    assert ext.position_stop_losses == []


def test_dry_run_go_off_confirmed_returns_without_exchange_calls(tmp_path) -> None:
    """dry_run 硬止损只打印意图，不撤单、不平仓、不撤 TPSL，也不重读持仓。"""
    ext = StopExt(positions=[Decimal("0.02")])
    eng = GridEngine(
        ext,
        GridConfig(
            dry_run=True,
            trend_aware=True,
            state_path=str(tmp_path / "s.json"),
        ),
    )

    result = asyncio.run(eng._go_off_confirmed(Decimal("0.02")))

    assert result is True
    assert ext.position_reads == 0
    assert ext.grid_cancelled == 0
    assert ext.market_orders == []
    assert ext.tpsl_cancelled == 0


def test_breach_freezes_and_does_not_recenter(tmp_path) -> None:
    """价格跌破下界后冻结 BUY，且既有 band 绝不追随现价移动。"""
    ext = StopExt(positions=[0.01], liq=(100.0, 80.0))
    cfg = GridConfig(
        dry_run=False,
        trend_aware=True,
        state_path=str(tmp_path / "s.json"),
    )
    eng = GridEngine(ext, cfg)
    eng._state = GridState(
        band_low=95.0,
        band_high=105.0,
        frozen=False,
        blocked_side=None,
        halted=False,
    )

    asyncio.run(eng._advance_band(mark=90.0, mode=GridMode.OFF))

    st = load_state(cfg.state_path)
    assert st is not None
    assert st.frozen is True
    assert st.blocked_side == "BUY"
    assert st.band_low == 95.0
    assert st.band_high == 105.0
    assert ext.grid_cancelled == 1


def test_mark_inside_band_does_not_freeze(tmp_path) -> None:
    """价格仍在 band 内时保持 ACTIVE，不撤网格单。"""
    ext = StopExt(positions=[0.0])
    cfg = GridConfig(
        dry_run=False,
        trend_aware=True,
        state_path=str(tmp_path / "s.json"),
    )
    eng = GridEngine(ext, cfg)
    eng._state = GridState(95.0, 105.0, False, None, False)

    asyncio.run(eng._advance_band(mark=100.0, mode=GridMode.NEUTRAL))

    assert eng._state == GridState(95.0, 105.0, False, None, False)
    assert ext.grid_cancelled == 0


def test_upper_breach_freezes_sell_without_recentering(tmp_path) -> None:
    """价格涨破上界后冻结 SELL，且 band 上下界保持原值。"""
    ext = StopExt(positions=[0.0])
    cfg = GridConfig(
        dry_run=True,
        trend_aware=True,
        state_path=str(tmp_path / "s.json"),
    )
    eng = GridEngine(ext, cfg)
    eng._state = GridState(95.0, 105.0, False, None, False)

    asyncio.run(eng._advance_band(mark=110.0, mode=GridMode.NEUTRAL))

    assert eng._state == GridState(95.0, 105.0, True, "SELL", False)
    assert load_state(cfg.state_path) == eng._state
    assert ext.grid_cancelled == 0


def test_frozen_band_recenters_after_required_neutral_bars(tmp_path) -> None:
    """空仓、无风险挂单且连续足够 NEUTRAL 后才重建 band。"""
    ext = StopExt(positions=[0.0])
    cfg = GridConfig(
        dry_run=False,
        trend_aware=True,
        band_k=1.75,
        min_half_frac=0.04,
        recenter_bars=2,
        state_path=str(tmp_path / "s.json"),
    )
    eng = GridEngine(ext, cfg)
    eng._state = GridState(95.0, 105.0, True, "BUY", False)
    eng._latest_atr = 1.0

    asyncio.run(eng._advance_band(mark=100.0, mode=GridMode.NEUTRAL))
    assert eng._state.frozen is True

    asyncio.run(eng._advance_band(mark=100.0, mode=GridMode.NEUTRAL))
    assert eng._state == GridState(96.0, 104.0, False, None, False)
    assert load_state(cfg.state_path) == eng._state


def test_maintain_ladder_respects_band_and_blocked_side(tmp_path) -> None:
    """ACTIVE 铺单只取 ladder 与 band 的交集，并跳过 blocked_side。"""
    ext = StopExt(positions=[0.0])
    cfg = GridConfig(
        dry_run=False,
        trend_aware=True,
        spacing_pct=0.02,
        unit_usd=1.0,
        max_inventory_usd=100.0,
        levels_per_side=3,
        state_path=str(tmp_path / "s.json"),
    )
    eng = GridEngine(ext, cfg)

    asyncio.run(
        eng._maintain_ladder(
            price=100.0,
            inv_usd=0.0,
            band=(98.0, 103.0),
            blocked_side="BUY",
        )
    )

    assert ext.placed
    assert all(order["side"] is Side.SELL for order in ext.placed)
    assert all(98.0 <= order["price"] <= 103.0 for order in ext.placed)


def test_connect_missing_state_with_position_fails_closed(tmp_path) -> None:
    """状态缺失但账户有仓时不得按现价新建 band。"""
    ext = StopExt(positions=[0.01])
    cfg = GridConfig(
        dry_run=True,
        trend_aware=True,
        state_path=str(tmp_path / "missing.json"),
    )
    eng = GridEngine(ext, cfg)

    asyncio.run(eng.connect())

    assert eng._state == GridState(0.0, 0.0, True, None, False)
    assert load_state(cfg.state_path) == eng._state


def test_connect_missing_state_with_open_order_fails_closed(tmp_path) -> None:
    """状态缺失但账户有挂单时同样 fail-closed。"""
    ext = StopExt(positions=[0.0])
    ext.open_orders = [
        SimpleNamespace(
            id="existing",
            price=100.0,
            side="BUY",
            reduce_only=False,
            type="LIMIT",
        )
    ]
    cfg = GridConfig(
        dry_run=True,
        trend_aware=True,
        state_path=str(tmp_path / "missing.json"),
    )
    eng = GridEngine(ext, cfg)

    asyncio.run(eng.connect())

    assert eng._state == GridState(0.0, 0.0, True, None, False)
    assert load_state(cfg.state_path) == eng._state


def test_connect_empty_account_allows_first_band_creation(tmp_path) -> None:
    """状态缺失且空仓无挂单时保持无状态，允许首轮创建 band。"""
    ext = StopExt(positions=[0.0])
    cfg = GridConfig(
        dry_run=True,
        trend_aware=True,
        state_path=str(tmp_path / "missing.json"),
    )
    eng = GridEngine(ext, cfg)

    asyncio.run(eng.connect())

    assert eng._state is None
    assert not (tmp_path / "missing.json").exists()


def test_connect_restores_persisted_band(tmp_path) -> None:
    """connect 读回持久化 band，重启后不得按现价重算。"""
    path = tmp_path / "s.json"
    expected = GridState(95.0, 105.0, True, "BUY", False)
    save_state(path, expected)
    ext = StopExt(positions=[0.0])
    eng = GridEngine(
        ext,
        GridConfig(
            dry_run=True,
            trend_aware=True,
            state_path=str(path),
        ),
    )

    asyncio.run(eng.connect())

    assert eng._state == expected


def test_default_path_unchanged(tmp_path, monkeypatch) -> None:
    """trend_aware=False 时 run_once 走原 decide_mode 路径且不触碰新逻辑。"""

    class GuardExt(RunExt):
        async def get_liquidation_info(self, market):
            raise AssertionError("默认路径不应查清算价")

        async def place_position_stop_loss(self, market, signed_size, trigger_price):
            raise AssertionError("默认路径不应维护 TPSL")

    async def fail_new_logic(*args, **kwargs):
        raise AssertionError("默认路径不应进入趋势感知逻辑")

    def fail_state_io(*args, **kwargs):
        raise AssertionError("默认路径不应读写趋势感知状态")

    decide_calls = []

    def fake_decide_mode(**kwargs):
        decide_calls.append(kwargs)
        return GridMode.NEUTRAL

    state_path = tmp_path / "grid_state.json"
    ext = GuardExt(positions=[0.0])
    eng = GridEngine(
        ext,
        GridConfig(
            dry_run=True,
            trend_aware=False,
            adx_period=1,
            donchian_period=1,
            levels_per_side=1,
            state_path=str(state_path),
        ),
    )
    monkeypatch.setattr(eng, "_run_once_trend_aware", fail_new_logic)
    monkeypatch.setattr(eng, "_check_hard_stop", fail_new_logic)
    monkeypatch.setattr(eng, "_maintain_tpsl", fail_new_logic)
    monkeypatch.setattr("grid.grid_engine.load_state", fail_state_io)
    monkeypatch.setattr("grid.grid_engine.save_state", fail_state_io)
    monkeypatch.setattr("grid.grid_engine.decide_mode", fake_decide_mode)

    asyncio.run(eng.run_once())

    assert len(decide_calls) == 1
    assert not state_path.exists()


def test_run_once_checks_hard_stop_before_candles(tmp_path) -> None:
    """硬止损触发后本轮立即返回，不能先拉 K 线或继续铺单。"""
    ext = RunExt(
        positions=[0.02, 0.02, 0.0, 0.0],
        liq=(100.0, 93.0),
    )
    eng = _eng(ext, tmp_path / "s.json")

    asyncio.run(eng.run_once())

    assert ext.calls[0] == "position"
    assert "candles" not in ext.calls
    assert ext.placed == []


def test_run_once_halted_returns_before_candles(tmp_path) -> None:
    """持久化 HALTED 状态禁止拉 K 线和任何新增报价。

    进程启动后第一次遇到 halted 会把确认平仓链跑一遍（查仓位/核残单），
    因为"空仓"不等于"收摊完成"——撤单或撤 TPSL 失败会留下普通挂单，
    成交后重新开仓。所以这里只锁死两条：不拉 K 线、不下新单。
    """
    ext = RunExt(positions=[0.0])
    eng = _eng(ext, tmp_path / "s.json")
    eng._state = GridState(95.0, 105.0, True, "BUY", True)

    result = asyncio.run(eng.run_once())

    assert result.startswith("HALTED")
    assert "candles" not in ext.calls
    assert ext.placed == []


def test_run_once_halted_settles_once_then_stops_retrying(tmp_path) -> None:
    """收摊确认跑通一次后就该停手，不能每 60 秒空转一遍确认链。"""
    ext = RunExt(positions=[0.0, 0.0, 0.0, 0.0, 0.0, 0.0])
    eng = _eng(ext, tmp_path / "s.json")
    eng._state = GridState(95.0, 105.0, True, "BUY", True)

    asyncio.run(eng.run_once())
    assert eng._halt_settled is True

    calls_after_first = len(ext.calls)
    eng._last_halt_retry_ts = 0.0          # 即便节流窗口到期
    asyncio.run(eng.run_once())
    # 已 settled，第二轮只该查一次仓位，不再重跑确认链
    assert len(ext.calls) - calls_after_first == 1


def test_run_once_drops_forming_candle_and_builds_active_band(
    tmp_path,
    monkeypatch,
) -> None:
    """趋势感知正常轮次只把已收盘 K 线交给 gate，并在 mark 周围建固定 band。"""
    ext = RunExt(positions=[0.0, 0.0], mark=100.0)
    eng = GridEngine(
        ext,
        GridConfig(
            dry_run=False,
            trend_aware=True,
            unit_usd=1.0,
            max_inventory_usd=100.0,
            levels_per_side=1,
            state_path=str(tmp_path / "s.json"),
        ),
    )
    seen = {}

    def fake_trend_gate(highs, lows, closes, **kwargs):
        seen["lengths"] = (len(highs), len(lows), len(closes))
        return GridMode.NEUTRAL

    monkeypatch.setattr("grid.grid_engine.trend_gate", fake_trend_gate)

    asyncio.run(eng.run_once())

    assert ext.calls[0] == "position"
    assert seen["lengths"] == (3, 3, 3)
    assert eng._state == GridState(96.0, 104.0, False, None, False)
    assert load_state(tmp_path / "s.json") == eng._state
    assert ext.placed
