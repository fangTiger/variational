"""网格 provider 测试。失败路径优先——文件缺失、损坏是常态，不能让面板白屏。"""

from __future__ import annotations

import json

from panel.providers.grid import collect


def _write(tmp_path, live: dict | None, monitor: list[dict] | None):
    live_path = tmp_path / "grid_live.json"
    monitor_path = tmp_path / "grid_monitor.jsonl"
    if live is not None:
        live_path.write_text(json.dumps(live), encoding="utf-8")
    if monitor is not None:
        monitor_path.write_text(
            "\n".join(json.dumps(r) for r in monitor), encoding="utf-8"
        )
    return live_path, monitor_path


_LIVE = {
    "ts": 1786946078.3,
    "mark": 63422.9,
    "mode": "neutral",
    "inv_btc": -0.0254,
    "inv_usd": -1610.9,
    "band_low": 61669.4,
    "band_high": 65484.0,
    "frozen": False,
    "halted": False,
    "dist_to_liq_pct": 0.6038,
    "cfg": {"unit": 300.0, "levels": 30, "max_inv": 3750.0, "spacing": 0.000986},
}
_MONITOR = [{"ts": 1786946078.3, "equity": 997.87, "pnl_since_start": 49.2}]


def test_missing_files_do_not_raise(tmp_path):
    """文件都不存在时返回一张「无数据」卡片，而不是抛异常。"""
    lp, mp = _write(tmp_path, None, None)
    s = collect(live_path=lp, monitor_path=mp)
    assert s.name
    assert s.error is not None


def test_corrupt_live_json_is_tolerated(tmp_path):
    lp, mp = _write(tmp_path, None, _MONITOR)
    lp.write_text("{ 这不是 json", encoding="utf-8")
    s = collect(live_path=lp, monitor_path=mp)
    assert s.error is not None


def test_healthy_snapshot_produces_metrics(tmp_path):
    lp, mp = _write(tmp_path, _LIVE, _MONITOR)
    s = collect(live_path=lp, monitor_path=mp)
    assert s.error is None
    assert s.equity == 997.87
    labels = [m.label for m in s.metrics]
    assert "权益" in labels
    assert "持仓" in labels
    assert "库存上限" in labels


def test_halted_is_marked_bad(tmp_path):
    """熔断是最要紧的状态，必须以 bad 色显示。"""
    lp, mp = _write(tmp_path, {**_LIVE, "halted": True}, _MONITOR)
    s = collect(live_path=lp, monitor_path=mp)
    tones = {m.label: m.tone for m in s.metrics}
    assert tones["熔断"] == "bad"


def test_frozen_is_marked_warn(tmp_path):
    lp, mp = _write(tmp_path, {**_LIVE, "frozen": True}, _MONITOR)
    s = collect(live_path=lp, monitor_path=mp)
    tones = {m.label: m.tone for m in s.metrics}
    assert tones["熔断"] == "warn"
