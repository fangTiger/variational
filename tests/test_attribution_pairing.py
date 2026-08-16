"""闭环配对测试。

配对规则与引擎内现行逻辑一致（grid/grid_engine.py:1388-1400）：
同一格出现反向成交即配对，数量取小值，剩余部分留待下次配对。
差别在于这里是离线跑完整成交流，跨重启不会断裂。
"""

from __future__ import annotations

from grid.attribution.pairing import pair_fills


def _f(fill_id, level, side, price, qty=1.0, ts=None):
    return {
        "fill_id": fill_id,
        "level": level,
        "side": side,
        "price": price,
        "qty": qty,
        "ts": ts if ts is not None else float(fill_id),
    }


def test_buy_then_sell_makes_one_loop():
    loops = pair_fills([
        _f("1", 100, "BUY", 100.0),
        _f("2", 100, "SELL", 101.0),
    ])
    assert len(loops) == 1
    assert loops[0]["buy_price"] == 100.0
    assert loops[0]["sell_price"] == 101.0
    assert loops[0]["gross_pnl"] == 1.0
    assert loops[0]["loop_id"] == "1+2"


def test_same_side_does_not_pair():
    loops = pair_fills([
        _f("1", 100, "BUY", 100.0),
        _f("2", 100, "BUY", 99.0),
    ])
    assert loops == []


def test_different_levels_do_not_pair():
    loops = pair_fills([
        _f("1", 100, "BUY", 100.0),
        _f("2", 101, "SELL", 101.0),
    ])
    assert loops == []


def test_partial_quantity_leaves_remainder():
    """数量取小值，剩余部分要能参与下一次配对。"""
    loops = pair_fills([
        _f("1", 100, "BUY", 100.0, qty=3.0),
        _f("2", 100, "SELL", 101.0, qty=1.0),
        _f("3", 100, "SELL", 102.0, qty=1.0),
    ])
    assert len(loops) == 2
    assert loops[0]["qty"] == 1.0
    assert loops[1]["qty"] == 1.0
    assert loops[1]["gross_pnl"] == 2.0


def test_pairs_across_engine_restarts():
    """核心价值：跨重启的半个闭环在离线配对里不会丢。"""
    loops = pair_fills([
        {**_f("1", 100, "BUY", 100.0), "engine_run_id": "run-1"},
        {**_f("2", 100, "SELL", 101.0), "engine_run_id": "run-2"},
    ])
    assert len(loops) == 1


def test_sell_then_buy_also_pairs():
    """空头方向同样成环：先卖后买。"""
    loops = pair_fills([
        _f("1", 100, "SELL", 101.0),
        _f("2", 100, "BUY", 100.0),
    ])
    assert len(loops) == 1
    assert loops[0]["gross_pnl"] == 1.0


def test_result_is_deterministic():
    """同一批输入必须产出同样的 loop_id，否则重复入库会重复计利润。"""
    fills = [
        _f("1", 100, "BUY", 100.0),
        _f("2", 100, "SELL", 101.0),
    ]
    assert pair_fills(fills) == pair_fills(fills)
