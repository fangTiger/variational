"""监控面板纯函数与渲染测试。"""
from __future__ import annotations

import asyncio
import json

from grid.grid_engine import GridConfig, GridEngine
from grid.regime import GridMode, close_slope, describe_regime


def test_close_slope_flat_is_zero() -> None:
    assert abs(close_slope([100.0] * 30, 20)) < 1e-9


def test_close_slope_up_is_positive() -> None:
    closes = [100.0 + i for i in range(30)]  # 每根 +1
    s = close_slope(closes, 20)
    assert s > 0
    # 归一化：每根涨幅 ≈ 1/当前价
    assert abs(s - (1.0 / closes[-1])) < 1e-6


def test_close_slope_down_is_negative() -> None:
    closes = [200.0 - i for i in range(30)]
    assert close_slope(closes, 20) < 0


def test_close_slope_short_series_returns_zero() -> None:
    assert close_slope([100.0, 101.0], 20) == 0.0


def test_describe_neutral_calm() -> None:
    s = describe_regime(GridMode.NEUTRAL, adx=22.0, slope_short=0.0, slope_long=0.0, atr_pct=0.004)
    assert "震荡" in s and "适合网格" in s


def test_describe_trend_off() -> None:
    s = describe_regime(GridMode.OFF, adx=35.0, slope_short=0.01, slope_long=0.008, atr_pct=0.02)
    assert "趋势" in s and "停铺" in s


def test_describe_mentions_slope_direction() -> None:
    up = describe_regime(GridMode.NEUTRAL, adx=20.0, slope_short=0.02, slope_long=0.02, atr_pct=0.004)
    assert "上行" in up
    down = describe_regime(GridMode.NEUTRAL, adx=20.0, slope_short=-0.02, slope_long=-0.02, atr_pct=0.004)
    assert "下行" in down


def test_dump_live_writes_expected_fields(tmp_path) -> None:
    live_path = tmp_path / "grid_live.json"
    cfg = GridConfig(
        dry_run=True,
        trend_aware=True,
        unit_usd=40,
        levels_per_side=4,
        max_inventory_usd=160,
        spacing_pct=0.005,
        hard_stop_dist=0.12,
        adx_off=30,
        state_path=str(tmp_path / "grid_state.json"),
    )
    eng = GridEngine(object(), cfg)
    eng._latest_atr = 250.0
    eng._last_dist_to_liq = None
    closes = [60000.0 + i for i in range(60)]

    asyncio.run(
        eng._dump_live(
            mark=63000.0,
            mode=GridMode.NEUTRAL,
            inv=0.0,
            closes=closes,
        )
    )

    d = json.loads(live_path.read_text(encoding="utf-8"))
    assert d["mark"] == 63000.0 and d["mode"] == "neutral"
    assert d["cfg"]["unit"] == 40 and d["cfg"]["max_inv"] == 160
    assert "adx" in d and "slope_short" in d and "atr_pct" in d
