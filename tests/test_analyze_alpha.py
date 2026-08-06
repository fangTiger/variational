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


def test_segment_sigma_excludes_cross_segment_returns() -> None:
    """σ 必须只用段内收益。

    回归：原实现把各段拼接后算 σ，段间跳变被当成真实收益，实测
    15 段、段间跳变 0.2% 的场景 σ 虚高 438%，窗口整体平移到把
    操作点排除在外。
    """
    import math

    from tools.analyze_alpha import segment_sigma

    # 两段各自平稳（段内收益恒为 0），段间隔一次 10% 跳变
    seg_a = [{"p": 100.0, "T": i} for i in range(50)]
    seg_b = [{"p": 110.0, "T": 1000 + i} for i in range(50)]
    # 段内全部收益为 0 → σ 应为 0；若把跳变算进去则显著大于 0
    assert segment_sigma([seg_a, seg_b]) == 0.0
    # 对照：拼接后算会因为那一次跳变而非零
    joined = [r["p"] for r in seg_a + seg_b]
    rets = [math.log(joined[i] / joined[i - 1]) for i in range(1, len(joined))]
    assert any(abs(r) > 0.01 for r in rets)


def test_adx_at_bisect_boundaries() -> None:
    from tools.analyze_alpha import adx_at

    series = [(1000.0, 15.0), (2000.0, 25.0), (3000.0, 35.0)]
    assert adx_at(series, 999_000) is None          # 早于首条
    assert adx_at(series, 1_000_000) == 15.0        # 恰好等于
    assert adx_at(series, 1_500_000) == 15.0        # 落在两条之间取前一条
    assert adx_at(series, 9_999_000) == 35.0        # 晚于末条
    assert adx_at([], 1_000) is None                 # 空序列


def test_split_by_regime_cuts_within_segment() -> None:
    """一段横跨 regime 边界时必须切成子段，不能整段归入段首那一档。"""
    from tools.analyze_alpha import split_by_regime

    series = [(1000.0, 15.0), (2000.0, 35.0)]       # 1000s 起震荡，2000s 起趋势
    seg = [{"p": 100.0 + i, "T": (1000 + i * 250) * 1000} for i in range(8)]
    buckets = split_by_regime([seg], series)
    assert "<20" in buckets and ">30" in buckets
    assert sum(len(s) for ss in buckets.values() for s in ss) <= len(seg)


def test_analyze_prints_diagnostics_but_no_advice_when_out_of_window(capsys) -> None:
    """操作点在窗口外时：诊断表要打印，方向性建议不许打印。

    窗口外恰恰是最需要诊断信息排查原因的时候。
    """
    import math
    import random
    from tools.analyze_alpha import analyze

    # 构造 σ=1e-5 的序列：窗口 [16σ, 64σ] = [0.016%, 0.064%]，
    # 整体低于操作点 0.0986%，但仍高于 tick 下界（8×1/64000 = 0.0125%），
    # 所以 usable_window 能正常建出窗口、只是操作点落在窗口外。
    # σ 再小会触发「无可信区间」而走不到这条分支。
    rng = random.Random(3)
    price = 64000.0
    seg = []
    for i in range(3000):
        price *= math.exp(rng.gauss(0.0, 1e-5))
        seg.append({"p": price, "T": 1786000000000 + i * 100})
    analyze([seg], "测试")
    out = capsys.readouterr().out
    assert "落在可信窗口外" in out
    assert "振荡数" in out                      # 诊断表仍要打印
    assert "→ 建议" not in out                  # 不许给方向性建议
