"""对冲 provider 测试。净敞口是这张卡最要紧的数，必须一眼看出对不对。"""

from __future__ import annotations

import json
import time

from panel.providers.hedge import collect


def _snap(**kw) -> dict:
    row = {
        "ts": time.time(),
        "interval": 30,
        "primary_size": "0.02362",
        "hedge_size": "-0.02362",
        "net_delta": "0.00000",
        "action": "无需再平衡",
        "primary_read_ok": True,
        "hedge_read_ok": True,
        "primary_notional_exceeded": False,
        "rebalance_threshold_ratio": "0.02",
        "hedge_free_margin_ratio": "0.78",
        "min_hedge_free_margin_ratio": "0.20",
        "hedge_margin_error": None,
    }
    row.update(kw)
    return row


def _write(tmp_path, rows):
    p = tmp_path / "lighter_hedge.jsonl"
    if rows is not None:
        p.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    return p


def test_missing_file_does_not_raise(tmp_path):
    s = collect(snapshot_path=_write(tmp_path, None))
    assert s.alive is False
    assert s.error is not None


def test_neutral_position_is_good(tmp_path):
    s = collect(snapshot_path=_write(tmp_path, [_snap()]))
    tones = {m.label: m.tone for m in s.metrics}
    assert tones["净敞口"] == "good"


def test_non_zero_net_delta_is_bad(tmp_path):
    """净敞口不为零意味着用户在裸奔，必须红色。"""
    s = collect(snapshot_path=_write(tmp_path, [_snap(hedge_size="0", net_delta="0.02362")]))
    tones = {m.label: m.tone for m in s.metrics}
    assert tones["净敞口"] == "bad"


def test_stale_heartbeat_marks_dead(tmp_path):
    """心跳超过 3×interval 视为失联。"""
    s = collect(snapshot_path=_write(tmp_path, [_snap(ts=time.time() - 300)]))
    assert s.alive is False


def test_equity_sums_both_legs(tmp_path):
    """两条腿是一个整体，权益取 Lighter 抵押品 + Extended 权益之和。"""
    s = collect(snapshot_path=_write(tmp_path, [
        _snap(primary_collateral="489.27", hedge_equity="465.23")
    ]))
    assert s.equity == 954.5


def test_equity_is_none_when_fields_missing(tmp_path):
    """老版本心跳没有权益字段时不参与汇总，也不能报错。"""
    s = collect(snapshot_path=_write(tmp_path, [_snap()]))
    assert s.equity is None


def test_equity_is_none_when_only_one_leg_available(tmp_path):
    """只有一条腿的数不能算总权益，那会少算一半。"""
    s = collect(snapshot_path=_write(tmp_path, [_snap(hedge_equity="465.23")]))
    assert s.equity is None
