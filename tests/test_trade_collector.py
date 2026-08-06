"""采集器纯逻辑测试：解析、去重、缺口检测。不联网。"""
from __future__ import annotations

from tools.trade_collector import TradeBuffer, parse_trades


def test_parse_trades_extracts_fields() -> None:
    msg = {
        "data": [
            {"i": 111, "m": "BTC-USD", "S": "BUY", "tT": "TRADE",
             "T": 1786003217916, "p": "64719", "q": "0.00011"},
        ]
    }
    assert parse_trades(msg) == [
        {"i": 111, "T": 1786003217916, "p": 64719.0, "q": 0.00011, "S": "BUY"}
    ]


def test_parse_trades_handles_empty() -> None:
    assert parse_trades({}) == []
    assert parse_trades({"data": []}) == []
    assert parse_trades({"data": None}) == []


def test_parse_trades_skips_malformed_row() -> None:
    # 缺字段的行跳过，不能让整条消息作废
    msg = {"data": [{"i": 1}, {"i": 2, "T": 5, "p": "10", "q": "1", "S": "BUY"}]}
    assert [r["i"] for r in parse_trades(msg)] == [2]


def test_parse_trades_skips_unparsable_price() -> None:
    msg = {"data": [{"i": 1, "T": 5, "p": "abc", "q": "1", "S": "BUY"}]}
    assert parse_trades(msg) == []


def test_buffer_deduplicates_by_id() -> None:
    buf = TradeBuffer()
    first = buf.add([{"i": 1, "T": 100, "p": 10.0, "q": 1.0, "S": "BUY"}])
    dup = buf.add([{"i": 1, "T": 100, "p": 10.0, "q": 1.0, "S": "BUY"}])
    assert len(first) == 1
    assert dup == []


def test_buffer_keeps_distinct_ids() -> None:
    buf = TradeBuffer()
    rows = [{"i": 1, "T": 100, "p": 10.0, "q": 1.0, "S": "BUY"},
            {"i": 2, "T": 101, "p": 11.0, "q": 1.0, "S": "SELL"}]
    assert len(buf.add(rows)) == 2


def test_buffer_detects_gap() -> None:
    buf = TradeBuffer(max_gap_ms=1000)
    buf.add([{"i": 1, "T": 1000, "p": 10.0, "q": 1.0, "S": "BUY"}])
    assert buf.gap_since(2500) == (1000, 2500)   # 间隔 1500ms > 1000ms
    assert buf.gap_since(1500) is None            # 间隔 500ms，正常


def test_buffer_gap_none_when_empty() -> None:
    assert TradeBuffer().gap_since(1000) is None


def test_buffer_tracks_latest_timestamp_not_last_added() -> None:
    # 乱序到达时，缺口判定必须基于最大时间戳而非最后一条
    buf = TradeBuffer(max_gap_ms=1000)
    buf.add([{"i": 1, "T": 5000, "p": 10.0, "q": 1.0, "S": "BUY"},
             {"i": 2, "T": 3000, "p": 10.0, "q": 1.0, "S": "BUY"}])
    assert buf.gap_since(5500) is None      # 距最大值 5000 只有 500ms
    assert buf.gap_since(6500) == (5000, 6500)
