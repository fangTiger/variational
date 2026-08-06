"""分析脚本的纯逻辑测试：缺口截断、ADX 分档。不读真实数据。"""
from __future__ import annotations

from tools.analyze_alpha import adx_bucket, segment_by_gap


def test_segment_by_gap_splits_at_marker() -> None:
    rows = [
        {"T": 1, "p": 10.0},
        {"T": 2, "p": 11.0},
        {"gap": True, "from": 2, "to": 999},
        {"T": 1000, "p": 50.0},
        {"T": 1001, "p": 51.0},
    ]
    segs = segment_by_gap(rows)
    assert [[r["p"] for r in s] for s in segs] == [[10.0, 11.0], [50.0, 51.0]]


def test_segment_by_gap_no_marker_single_segment() -> None:
    rows = [{"T": 1, "p": 10.0}, {"T": 2, "p": 11.0}]
    assert len(segment_by_gap(rows)) == 1


def test_segment_by_gap_drops_too_short_segments() -> None:
    # 单点段无法贡献振荡，直接丢弃
    rows = [
        {"T": 1, "p": 10.0},
        {"gap": True, "from": 1, "to": 5},
        {"T": 6, "p": 20.0},
        {"T": 7, "p": 21.0},
    ]
    segs = segment_by_gap(rows)
    assert len(segs) == 1
    assert [r["p"] for r in segs[0]] == [20.0, 21.0]


def test_segment_by_gap_handles_trailing_gap() -> None:
    # 缺口在末尾，不应留下空段
    rows = [{"T": 1, "p": 10.0}, {"T": 2, "p": 11.0},
            {"gap": True, "from": 2, "to": 99}]
    assert len(segment_by_gap(rows)) == 1


def test_adx_bucket_boundaries() -> None:
    assert adx_bucket(15.0) == "<20"
    assert adx_bucket(19.99) == "<20"
    assert adx_bucket(20.0) == "20-30"
    assert adx_bucket(30.0) == ">30"
    assert adx_bucket(35.0) == ">30"
    assert adx_bucket(None) == "unknown"
