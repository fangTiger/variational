"""采集器纯逻辑测试：解析、去重、缺口检测。不联网。"""
from __future__ import annotations

import asyncio
import json

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


def test_recover_recent_ids_skips_gap_lines(tmp_path) -> None:
    """恢复 id 时必须跳过 gap 标记行。"""
    from tools.trade_collector import recover_recent_ids
    f = tmp_path / "2026-08-06.jsonl"
    f.write_text(
        '{"i":1,"T":1000,"p":10.0,"q":1.0,"S":"BUY"}\n'
        '{"gap":true,"from":1,"to":2}\n'
        '{"i":2,"T":2000,"p":11.0,"q":1.0,"S":"SELL"}\n',
        encoding="utf-8",
    )
    assert recover_recent_ids(tmp_path) == [1, 2]


def test_restored_seen_ids_dedup_backlog(tmp_path) -> None:
    """重启后流补发的 backlog 不得重复落盘。

    实测公开成交流连上时会补发约 50 笔 / 57 秒历史；不恢复 _seen 会让
    这批已落盘成交被重复写入，抬高下游振荡计数。
    """
    from tools.trade_collector import recover_recent_ids
    f = tmp_path / "2026-08-06.jsonl"
    f.write_text(
        "".join(
            '{"i":%d,"T":%d,"p":10.0,"q":1.0,"S":"BUY"}\n' % (i, 1_000_000 + i)
            for i in range(1, 81)
        ),
        encoding="utf-8",
    )
    buf = TradeBuffer(seen_ids=recover_recent_ids(tmp_path))
    backlog = [{"i": i, "T": 1_000_000 + i, "p": 10.0, "q": 1.0, "S": "BUY"}
               for i in range(51, 81)]
    live = [{"i": i, "T": 1_000_000 + i, "p": 10.0, "q": 1.0, "S": "BUY"}
            for i in range(81, 86)]
    assert buf.add(backlog) == []          # backlog 全部去重
    assert len(buf.add(live)) == 5         # 新数据照常


def test_run_forever_keeps_pending_break_through_empty_message(monkeypatch) -> None:
    """重连后首批消息为空时，pending_break 不得被提前清除。

    clear_discontinuity() 在 `if not rows: continue` 之后才执行，所以空
    消息不会误清断点标记；真正到来的第一批有效数据仍应触发一次缺口
    标记。用假 WebSocket 固化这条路径——不联网、不碰真实 data/trades/
    （_append/_append_gap 全部打桩，从不触碰磁盘）。
    """
    import tools.trade_collector as collector

    gap_calls: list[tuple[int, int]] = []
    appended: list[list[dict]] = []

    monkeypatch.setattr(collector, "recover_last_ts", lambda: 1000)
    monkeypatch.setattr(collector, "recover_recent_ids", lambda: [])
    monkeypatch.setattr(collector, "_append_gap",
                         lambda start, end: gap_calls.append((start, end)))
    monkeypatch.setattr(collector, "_append", lambda rows: appended.append(rows))

    empty_msg = json.dumps({"data": []})
    real_msg_1 = json.dumps({"data": [
        {"i": 1, "T": 5000, "p": "10", "q": "1", "S": "BUY"}
    ]})
    real_msg_2 = json.dumps({"data": [
        {"i": 2, "T": 5100, "p": "10", "q": "1", "S": "BUY"}
    ]})

    class _FakeConnection:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc_info):
            return False

        def __aiter__(self):
            return self._gen()

        async def _gen(self):
            yield empty_msg
            yield real_msg_1
            yield real_msg_2
            raise asyncio.CancelledError  # 测试到此为止，让 run_forever 干净退出

    monkeypatch.setattr(collector.websockets, "connect",
                         lambda *a, **k: _FakeConnection())

    async def _drive() -> None:
        try:
            await collector.run_forever()
        except asyncio.CancelledError:
            pass

    asyncio.run(_drive())

    # 空消息不清断点：真正的第一批数据（5000）仍应触发一次缺口标记
    assert gap_calls == [(1000, 5000)]
    # 两批有效数据都正常落盘（打桩），第二批不再触发缺口
    assert len(appended) == 2
    assert [r["i"] for r in appended[0]] == [1]
    assert [r["i"] for r in appended[1]] == [2]
