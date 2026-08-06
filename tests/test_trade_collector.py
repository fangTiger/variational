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
    # 没有前序数据时不标记——无从谈起缺口，不是漏判。
    # 进程重启的缺口靠 recover_last_ts + mark_discontinuity 覆盖。
    assert TradeBuffer().gap_since(1000) is None


def test_buffer_tracks_latest_timestamp_not_last_added() -> None:
    # 乱序到达时，缺口判定必须基于最大时间戳而非最后一条
    buf = TradeBuffer(max_gap_ms=1000)
    buf.add([{"i": 1, "T": 5000, "p": 10.0, "q": 1.0, "S": "BUY"},
             {"i": 2, "T": 3000, "p": 10.0, "q": 1.0, "S": "BUY"}])
    assert buf.gap_since(5500) is None      # 距最大值 5000 只有 500ms
    assert buf.gap_since(6500) == (5000, 6500)


def test_buffer_marks_gap_after_discontinuity_regardless_of_duration() -> None:
    """断线重连后必须标记，哪怕中断时长远低于时间阈值。

    回归：原实现只看时间阈值，断线 30 秒（< 60 秒阈值）不标记，
    但数据确实丢了——漏标会让下游跨缺口拼接，凭空造出价格跳变。
    """
    buf = TradeBuffer(max_gap_ms=60_000)
    buf.add([{"i": 1, "T": 1_000_000, "p": 10.0, "q": 1.0, "S": "BUY"}])
    assert buf.gap_since(1_030_000) is None      # 未标断点时不触发
    buf.mark_discontinuity()
    assert buf.gap_since(1_030_000) == (1_000_000, 1_030_000)


def test_buffer_clear_discontinuity_stops_marking() -> None:
    buf = TradeBuffer(max_gap_ms=60_000)
    buf.add([{"i": 1, "T": 1_000_000, "p": 10.0, "q": 1.0, "S": "BUY"}])
    buf.mark_discontinuity()
    assert buf.gap_since(1_010_000) is not None
    buf.clear_discontinuity()
    assert buf.gap_since(1_010_000) is None


def test_buffer_restored_last_ts_enables_restart_gap() -> None:
    """进程重启场景：原实现无论停机多久检出率都是 0%。"""
    buf = TradeBuffer(last_ts=1_005_000)
    buf.mark_discontinuity()
    assert buf.gap_since(1_005_000 + 3_600_000) == (1_005_000, 4_605_000)


def test_buffer_seen_ids_are_bounded() -> None:
    """无界 set 跑 30 天会累积到 100MB+。"""
    from tools.trade_collector import MAX_SEEN_IDS
    buf = TradeBuffer()
    buf.add([{"i": i, "T": i, "p": 1.0, "q": 1.0, "S": "BUY"}
             for i in range(MAX_SEEN_IDS + 1000)])
    assert len(buf._seen) == MAX_SEEN_IDS


def test_recover_last_ts_skips_gap_lines(tmp_path) -> None:
    """恢复时必须跳过 gap 标记行，只取真实成交。"""
    from tools.trade_collector import recover_last_ts
    f = tmp_path / "2026-08-06.jsonl"
    f.write_text(
        '{"i":1,"T":1000000,"p":10.0,"q":1.0,"S":"BUY"}\n'
        '{"i":2,"T":1005000,"p":11.0,"q":1.0,"S":"SELL"}\n'
        '{"gap":true,"from":900000,"to":1000000}\n',
        encoding="utf-8",
    )
    assert recover_last_ts(tmp_path) == 1005000


def test_recover_last_ts_missing_dir(tmp_path) -> None:
    from tools.trade_collector import recover_last_ts
    assert recover_last_ts(tmp_path / "nonexistent") is None
