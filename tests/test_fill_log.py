"""成交追写测试。

引擎是实盘交易进程，这里的任何异常都不能冒泡到交易循环——
写盘失败宁可丢一条统计数据，也不能影响下单。
"""

from __future__ import annotations

import json
from decimal import Decimal

from grid.fill_log import append_fill, build_fill_record


def test_build_record_has_all_required_fields():
    rec = build_fill_record(
        fill_id="12345",
        ts=1786550000.0,
        level=11225,
        side="BUY",
        price=Decimal("64000.5"),
        qty=Decimal("0.0026"),
        engine_run_id="run-abc",
    )
    assert rec == {
        "fill_id": "12345",
        "ts": 1786550000.0,
        "level": 11225,
        "side": "BUY",
        "price": 64000.5,
        "qty": 0.0026,
        "engine_run_id": "run-abc",
    }


def test_append_writes_one_json_line(tmp_path):
    path = tmp_path / "fills.jsonl"
    rec = build_fill_record(
        fill_id="1",
        ts=1.0,
        level=2,
        side="SELL",
        price=Decimal("3"),
        qty=Decimal("4"),
        engine_run_id="r",
    )
    append_fill(path, rec)
    append_fill(path, rec)

    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["fill_id"] == "1"


def test_append_never_raises(tmp_path):
    """只读目录导致真实写盘失败时，异常也不能冒泡。"""
    bad_path = tmp_path / "readonly" / "fills.jsonl"
    bad_path.parent.mkdir()
    bad_path.parent.chmod(0o500)
    try:
        append_fill(bad_path, {"fill_id": "1"})
        assert not bad_path.exists()
    finally:
        bad_path.parent.chmod(0o700)
